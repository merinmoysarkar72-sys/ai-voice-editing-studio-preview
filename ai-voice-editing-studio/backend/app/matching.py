"""Conservative script-to-transcript alignment.

Exact token matches are preferred; bounded Unicode-aware fuzzy alignment tolerates
small ASR spelling errors. Uncertain words stay reviewable and are not silently cut.
"""
from __future__ import annotations
import re
import unicodedata
from pathlib import Path
from difflib import SequenceMatcher
from typing import Any

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?।؟])\s+|\n{1,}")


def normalize_token(token: str) -> str:
    return unicodedata.normalize("NFKC", token).casefold().strip()


def tokens(text: str) -> list[str]:
    # Python's standard ``\w`` excludes many Unicode combining marks. Bengali
    # vowel signs and virama therefore split one orthographic word into fragments
    # unless marks are explicitly kept with their base letters.
    result, current = [], []
    for char in unicodedata.normalize("NFKC", text):
        category = unicodedata.category(char)
        if char.isalnum() or category.startswith("M"):
            current.append(char)
        elif char in {"'", "’"} and current:
            current.append(char)
        else:
            if current:
                result.append(normalize_token("".join(current)))
                current = []
    if current:
        result.append(normalize_token("".join(current)))
    return result


def split_script(text: str) -> list[str]:
    raw = [s.strip() for s in SENTENCE_SPLIT_RE.split(text.replace("\r", "\n"))]
    return [s for s in raw if tokens(s)]


def _word_rows(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for segment in segments:
        words = segment.get("words") or []
        if words:
            for word in words:
                raw = (word.get("word") or "").strip()
                for token in tokens(raw):
                    rows.append({
                        "token": token,
                        "start": float(word.get("start", segment.get("start", 0))),
                        "end": float(word.get("end", segment.get("end", 0))),
                        "probability": float(word.get("probability", segment.get("avg_logprob", 0.8)) or 0.0),
                    })
        else:
            raw_tokens = tokens(segment.get("text", ""))
            if not raw_tokens:
                continue
            start = float(segment.get("start", 0))
            end = float(segment.get("end", start))
            span = max(0.0, end - start)
            for i, token in enumerate(raw_tokens):
                rows.append({
                    "token": token,
                    "start": start + span * i / len(raw_tokens),
                    "end": start + span * (i + 1) / len(raw_tokens),
                    "probability": 0.65,
                })
    return rows



def _token_similarity(a: str, b: str) -> float:
    if a == b: return 1.0
    if min(len(a),len(b)) <= 2: return 0.0
    return SequenceMatcher(None,a,b,autojunk=False).ratio()


def _fuzzy_token_alignment(target: list[str], spoken: list[str]) -> tuple[float, float, int]:
    """Order-preserving token alignment tolerant of small ASR spelling errors.

    Very short tokens require exact equality to avoid matching Bengali/English
    particles and articles too aggressively.
    """
    n, m = len(target), len(spoken)
    if not n or not m: return 0.0, 0.0, 0
    dp = [[0.0]*(m+1) for _ in range(n+1)]
    back = [[None]*(m+1) for _ in range(n+1)]
    for i in range(1,n+1): dp[i][0] = -0.42*i; back[i][0] = "target"
    for j in range(1,m+1): dp[0][j] = -0.32*j; back[0][j] = "spoken"
    similarities = {}
    for i in range(1,n+1):
        for j in range(1,m+1):
            a,b=target[i-1],spoken[j-1]
            sim = _token_similarity(a,b)
            similarities[(i,j)] = sim
            options=[(dp[i-1][j]-0.42,"target"),(dp[i][j-1]-0.32,"spoken"),(dp[i-1][j-1]+(sim*1.35-0.35 if sim>=0.55 else -0.75),"match")]
            dp[i][j],back[i][j]=max(options,key=lambda x:x[0])
    i,j=n,m; matches=[]
    while i or j:
        step=back[i][j]
        if step=="match":
            sim=similarities[(i,j)]
            if sim>=0.55: matches.append(sim)
            i-=1;j-=1
        elif step=="target": i-=1
        elif step=="spoken": j-=1
        else: break
    weighted=sum(matches)/n
    precision=len(matches)/m
    f1=(2*weighted*precision/(weighted+precision)) if weighted+precision else 0.0
    return min(1.0,weighted),min(1.0,f1),len(matches)


def _rank_take_candidates(target: list[str], audio_words: list[dict], limit: int = 6, after: float = -1.0) -> list[dict]:
    """Find alternate spoken takes and rank completeness before acoustic confidence.

    This is conservative text/timestamp evidence, not a phoneme-level pronunciation
    judge. Candidates below the match threshold are not auto-selected.
    """
    if not target or not audio_words:
        return []
    n = len(target)
    min_len, max_len = max(1, int(n * 0.68)), max(1, int(n * 1.30) + 1)
    candidates = []
    # Do not stitch two takes together across a conspicuous pause. A 550 ms
    # discontinuity is intentionally conservative; ambiguous phrase breaks remain
    # review candidates rather than becoming synthetic best-take matches.
    runs, run_start = [], 0
    for i in range(1, len(audio_words)):
        if audio_words[i]["start"] - audio_words[i-1]["end"] > 0.55:
            runs.append((run_start, i)); run_start = i
    runs.append((run_start, len(audio_words)))
    for run_a, run_b in runs:
      values = [w["token"] for w in audio_words[run_a:run_b]]
      # A candidate must begin at or immediately before a plausible first-word
      # occurrence. This avoids scanning every possible sliding window for every
      # script sentence on long recordings.
      first_positions = [i for i, value in enumerate(values) if _token_similarity(target[0], value) >= 0.55]
      starts = set(first_positions)
      starts.update(i-1 for i in first_positions if i > 0)
      if not starts and len(target) > 1:
          # If the first word was omitted by ASR, anchor on the second word and
          # allow the candidate window to begin one token earlier.
          second_positions=[i for i,value in enumerate(values) if _token_similarity(target[1],value)>=0.55]
          starts.update(i-1 for i in second_positions if i>0)
          starts.update(i-2 for i in second_positions if i>1)
      if not starts and len(values) <= 300:
          starts = set(range(len(values)))
      for local_start in sorted(starts):
        for size in range(min_len, max_len + 1):
            if local_start + size > len(values):
                continue
            start = run_a + local_start
            if audio_words[start]["start"] < after - 0.08:
                continue
            end = start + size
            chunk = values[local_start:local_start+size]
            coverage, ratio, matching = _fuzzy_token_alignment(target, chunk)
            if ratio < 0.64 or coverage < 0.62:
                continue
            words = audio_words[start:end]
            duration = max(0.01, words[-1]["end"] - words[0]["start"])
            rate = size / duration
            timing = 1.0 if 1.2 <= rate <= 5.5 else max(0.0, 1.0 - abs(rate - 3.0) / 6.0)
            probability = sum(w.get("probability", 0.5) for w in words) / len(words)
            completeness = min(1.0, size / n)
            score = 0.55 * coverage + 0.20 * probability + 0.15 * completeness + 0.10 * timing
            candidates.append({"start": round(words[0]["start"], 3), "end": round(words[-1]["end"], 3),
                "word_start": start, "word_end": end, "coverage": round(coverage, 4),
                "confidence": round(min(1.0, score), 4), "score": round(score, 4),
                "avg_word_probability": round(probability, 4), "speech_rate_wps": round(rate, 2),
                "recognized": " ".join(chunk), "complete": coverage >= 0.72 and completeness >= 0.88})
    candidates = [c for c in candidates if c["start"] >= after - 0.08]
    candidates.sort(key=lambda x: (x["score"], x["complete"], -x["start"]), reverse=True)
    chosen = []
    for item in candidates:
        if any(min(item["end"], old["end"]) - max(item["start"], old["start"]) > 0.55 * min(item["end"]-item["start"], old["end"]-old["start"]) for old in chosen):
            continue
        chosen.append(item)
        if len(chosen) >= limit:
            break
    return sorted(chosen, key=lambda x: x["start"])


def build_report(script_text: str, transcript_segments: list[dict], duration: float, silence_ranges: list[dict] | None = None, audio_path: Path | None = None) -> dict:
    sentences = split_script(script_text)
    script_tokens: list[dict] = []
    sentence_ranges = []
    for sentence_index, sentence in enumerate(sentences):
        sentence_words = tokens(sentence)
        start = len(script_tokens)
        script_tokens.extend({"token": t, "sentence_index": sentence_index} for t in sentence_words)
        sentence_ranges.append((start, len(script_tokens)))

    audio_words = _word_rows(transcript_segments)
    script_values = [x["token"] for x in script_tokens]
    audio_values = [x["token"] for x in audio_words]
    matcher = SequenceMatcher(None, script_values, audio_values, autojunk=False)
    mapping: dict[int, int] = {}
    audio_matched: set[int] = set()
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            si = block.a + offset
            ai = block.b + offset
            mapping[si] = ai
            audio_matched.add(ai)

    sentence_rows = []
    accepted_defaults = []
    matched_token_total = len(mapping)
    cursor = 0
    for idx, sentence in enumerate(sentences):
        s0, s1 = sentence_ranges[idx]
        mapped = [(si, mapping[si]) for si in range(s0, s1) if si in mapping]
        coverage = len(mapped) / max(1, s1 - s0)
        audio_indices = [ai for _, ai in mapped]
        times = [audio_words[ai] for ai in audio_indices]
        start = min((w["start"] for w in times), default=None)
        end = max((w["end"] for w in times), default=None)
        contiguous = bool(audio_indices) and all(b >= a for a, b in zip(audio_indices, audio_indices[1:]))
        # Split at unmatched spoken words inside a sentence. These ranges let the
        # export omit confirmed stumbles while retaining original recorded words.
        audio_ranges = []
        if audio_indices:
            run_start = audio_indices[0]
            run_end = audio_indices[0]
            for ai in audio_indices[1:]:
                if ai == run_end + 1:
                    run_end = ai
                else:
                    audio_ranges.append({"start": round(audio_words[run_start]["start"], 3), "end": round(audio_words[run_end]["end"], 3)})
                    run_start = run_end = ai
            audio_ranges.append({"start": round(audio_words[run_start]["start"], 3), "end": round(audio_words[run_end]["end"], 3)})
        avg_prob = (sum(w["probability"] for w in times) / len(times)) if times else 0.0
        confidence = min(1.0, coverage * 0.82 + avg_prob * 0.18) if times else 0.0
        if coverage >= 0.88 and contiguous and start is not None:
            status = "matched"
            include = True
            accepted_defaults.append(idx)
        elif coverage >= 0.45 and start is not None:
            status = "review"
            include = False
        else:
            status = "missing"
            include = False
        sentence_rows.append({
            "index": idx,
            "script": sentence,
            "recognized": " ".join(audio_words[ai]["token"] for ai in audio_indices),
            "coverage": round(coverage, 4),
            "confidence": round(confidence, 4),
            "status": status,
            "include": include,
            "start": round(start, 3) if start is not None else None,
            "end": round(end, 3) if end is not None else None,
            "audio_ranges": audio_ranges,
            "word_count": s1 - s0,
            "matched_words": len(mapped),
            "contiguous": contiguous,
        })

    # Discover candidate takes in script order. A provisional text score establishes
    # the next sentence's lower time bound; signal metrics are then added before the
    # final ranking pass. The audio path is optional for unit-only matching.
    repetition_counts = {}
    for row in sentence_rows:
        key = tuple(tokens(row["script"]))
        repetition_counts[key] = repetition_counts.get(key, 0) + 1
    previous_take_end = -1.0
    for row in sentence_rows:
        key = tuple(tokens(row["script"]))
        repeated_intent = repetition_counts.get(key, 0) > 1
        candidates = _rank_take_candidates(tokens(row["script"]), audio_words, after=previous_take_end)
        row["take_candidates"] = candidates
        if repeated_intent and row.get("status") == "matched" and row.get("start") is not None:
            previous_take_end = max(previous_take_end, float(row.get("end") or 0))
        elif candidates:
            provisional = max(candidates, key=lambda c: (c["complete"], c["score"], -c["start"]))
            if provisional["complete"] and provisional["confidence"] >= row["confidence"]:
                previous_take_end = max(previous_take_end, provisional["end"])
            elif row.get("start") is not None:
                previous_take_end = max(previous_take_end, float(row.get("end") or 0))
        elif row.get("start") is not None:
            previous_take_end = max(previous_take_end, float(row.get("end") or 0))

    signal_metrics = {}
    if audio_path is not None:
        take_map = {}
        for row in sentence_rows:
            for candidate in row.get("take_candidates", []):
                take_map[f"{candidate['start']:.3f}:{candidate['end']:.3f}"] = {"start":candidate["start"],"end":candidate["end"]}
        if take_map:
            from .audio import analyze_take_signal_quality
            signal_metrics = analyze_take_signal_quality(audio_path, list(take_map.values()))
    for row in sentence_rows:
        for candidate in row.get("take_candidates", []):
            key=f"{candidate['start']:.3f}:{candidate['end']:.3f}"
            metric=signal_metrics.get(key)
            if metric:
                candidate["audio_quality"] = metric
                candidate["ranking_score"] = round(.88*candidate["score"]+.12*metric["signal_score"],4)
            else:
                candidate["ranking_score"] = candidate["score"]

    previous_selected_end = -1.0
    for row in sentence_rows:
        key = tuple(tokens(row["script"]))
        repeated_intent = repetition_counts.get(key, 0) > 1
        if repeated_intent and row.get("status") == "matched" and row.get("start") is not None:
            nearest = min(row["take_candidates"], key=lambda c: abs(c["start"]-row["start"])) if row["take_candidates"] else None
            if nearest: row["selected_take"] = nearest
            row["best_take_auto_selected"] = False
            previous_selected_end = max(previous_selected_end, float(row.get("end") or 0))
            continue
        candidates = [c for c in row.get("take_candidates", []) if c["start"] >= previous_selected_end - 0.08]
        if candidates:
            best = max(candidates, key=lambda c: (c["complete"], c.get("ranking_score",c["score"]), -c["start"]))
            row["selected_take"] = best
            if best["confidence"] >= row["confidence"] and best["complete"]:
                row["start"], row["end"] = best["start"], best["end"]
                row["audio_ranges"] = [{"start": best["start"], "end": best["end"]}]
                row["recognized"] = best["recognized"]
                row["confidence"] = best["confidence"]
                row["coverage"] = best["coverage"]
                row["matched_words"] = len(tokens(row["script"]))
                row["contiguous"] = True
                audio_matched.update(range(int(best["word_start"]), int(best["word_end"])))
                if best["confidence"] >= 0.80:
                    row["status"] = "matched"
                    row["include"] = True
                row["best_take_auto_selected"] = True
            else:
                row["best_take_auto_selected"] = False
            previous_selected_end = max(previous_selected_end, best["end"])
        else:
            row["best_take_auto_selected"] = False
            if row.get("start") is not None:
                previous_selected_end = max(previous_selected_end, float(row.get("end") or 0))


    extras = [w for i, w in enumerate(audio_words) if i not in audio_matched]
    extra_groups = []
    for word in extras:
        if extra_groups and word["start"] - extra_groups[-1]["end"] <= 1.6:
            extra_groups[-1]["end"] = word["end"]
            extra_groups[-1]["words"].append(word["token"])
        else:
            extra_groups.append({"start": word["start"], "end": word["end"], "words": [word["token"]]})

    # Adjacent repeated tokens / short repeated phrases are surfaced as stutter
    # candidates. They remain review-only because rhetorical repetition is possible.
    stutters = []
    for i in range(1, len(audio_words)):
        if audio_words[i]["token"] == audio_words[i-1]["token"] and audio_words[i]["start"] - audio_words[i-1]["end"] < 0.8:
            stutters.append({"start": round(audio_words[i-1]["start"], 3), "end": round(audio_words[i]["end"], 3),
                             "text": f"{audio_words[i-1]['token']} {audio_words[i]['token']}", "category": "possible_stutter", "severity": "review"})

    # Long gaps between recognized words are flagged, but never auto-cut here.
    pauses = []
    for left, right in zip(audio_words, audio_words[1:]):
        gap = right["start"] - left["end"]
        if gap >= 1.5:
            pauses.append({"start": round(left["end"], 3), "end": round(right["start"], 3), "duration": round(gap, 3), "category": "long_pause"})

    accepted_defaults = [row["index"] for row in sentence_rows if row.get("include")]
    matched_count = sum(1 for row in sentence_rows if row["status"] == "matched")
    missing_count = sum(1 for row in sentence_rows if row["status"] == "missing")
    review_count = sum(1 for row in sentence_rows if row["status"] == "review")
    weighted_matched_tokens = sum(float(row.get("coverage", 0.0)) * int(row.get("word_count", 0)) for row in sentence_rows)
    match_pct = (weighted_matched_tokens / max(1, len(script_tokens))) * 100
    selected_rows = [row for row in sentence_rows if row["status"] == "matched"]
    estimated_final_duration = sum(max(0.0, r["end"] - r["start"]) for row in selected_rows for r in row.get("audio_ranges", []))
    if len(selected_rows) > 1:
        estimated_final_duration += 0.25 * (len(selected_rows) - 1)
    silence_ranges = silence_ranges or []
    issues = []
    script_sentence_tokens = [tokens(sentence) for sentence in sentences]
    for group in extra_groups:
        phrase = group["words"]
        repeat_candidate = any(len(phrase) >= 2 and phrase == sentence_tokens for sentence_tokens in script_sentence_tokens)
        issues.append({"category": "accidental_repeat_candidate" if repeat_candidate else "extra_or_repeated_speech", "start": round(group["start"], 3), "end": round(group["end"], 3), "text": " ".join(group["words"]), "severity": "review"})
    for pause in pauses:
        issues.append({**pause, "text": f"{pause['duration']:.1f}s pause", "severity": "review"})
    issues.extend(stutters)
    for silence in silence_ranges:
        if silence.get("duration", 0) >= 2.0:
            issues.append({"category": "silence", **silence, "severity": "review"})

    repetition_counts = {}
    for sentence_tokens in script_sentence_tokens:
        key = tuple(sentence_tokens)
        repetition_counts[key] = repetition_counts.get(key, 0) + 1
    intentional_repetitions = sum(count - 1 for count in repetition_counts.values() if count > 1)
    return {
        "language": "multilingual",
        "duration": round(float(duration), 3),
        "script_sentence_count": len(sentences),
        "matched_sentences": matched_count,
        "selected_sentences": len(accepted_defaults),
        "review_sentences": review_count,
        "missing_sentences": missing_count,
        "extra_speech_groups": len(extra_groups),
        "long_pauses": len(pauses),
        "possible_stutters": len(stutters),
        "intentional_repetition_occurrences": intentional_repetitions,
        "best_takes_auto_selected": sum(1 for row in sentence_rows if row.get("best_take_auto_selected")),
        "take_signal_metrics": len(signal_metrics),
        "uncertain_segments": review_count + sum(1 for issue in issues if issue.get("severity") == "review"),
        "matching_percentage": round(match_pct, 1),
        "estimated_final_duration": round(estimated_final_duration, 2),
        "sentences": sentence_rows,
        "issues": sorted(issues, key=lambda x: x.get("start", 0)),
        "transcript": " ".join(w["token"] for w in audio_words),
        "default_selected_sentence_indices": accepted_defaults,
        "notes": [
            "Only high-coverage, in-order exact word matches are preselected.",
            "Repeated script occurrences are aligned by sequence position and are not deduplicated.",
            "Unmatched speech and uncertain sentences require review before export.",
        ],
    }
