from backend.app.matching import build_report


def word(text, start, end, p=0.95):
    return {"word": text, "start": start, "end": end, "probability": p}


def segment(words):
    return {"start": words[0]["start"], "end": words[-1]["end"], "text": " ".join(w["word"] for w in words), "words": words}


def test_intentional_repeated_script_sentences_are_preserved_in_order():
    script = "I am ready. We can begin. I am ready."
    rows = [
        word("I", 0.0, 0.1), word("am", 0.11, 0.2), word("ready.", 0.21, 0.5),
        word("We", 1.0, 1.1), word("can", 1.11, 1.2), word("begin.", 1.21, 1.5),
        word("I", 2.0, 2.1), word("am", 2.11, 2.2), word("ready.", 2.21, 2.5),
    ]
    report = build_report(script, [segment(rows)], 3.0)
    assert report["script_sentence_count"] == 3
    assert report["matched_sentences"] == 3
    assert report["extra_speech_groups"] == 0
    assert [x["status"] for x in report["sentences"]] == ["matched"] * 3


def test_duplicate_audio_take_is_reported_as_extra_and_not_a_second_script_line():
    script = "I am ready."
    rows = [
        word("I", 0.0, 0.1), word("am", 0.11, 0.2), word("ready.", 0.21, 0.5),
        word("I", 1.0, 1.1), word("am", 1.11, 1.2), word("ready.", 1.21, 1.5),
    ]
    report = build_report(script, [segment(rows)], 2.0)
    assert report["script_sentence_count"] == 1
    assert report["matched_sentences"] == 1
    assert report["extra_speech_groups"] >= 1
    assert report["issues"][0]["category"] == "accidental_repeat_candidate"


def test_missing_script_sentence_is_not_fabricated():
    script = "I am ready. The recording is finished."
    rows = [word("I", 0.0, 0.1), word("am", 0.11, 0.2), word("ready.", 0.21, 0.5)]
    report = build_report(script, [segment(rows)], 1.0)
    assert report["missing_sentences"] == 1
    missing = report["sentences"][1]
    assert missing["status"] == "missing"
    assert missing["include"] is False
    assert missing["start"] is None


def test_unmatched_stutter_words_split_audio_edit_ranges():
    script = "I am ready now."
    rows = [
        word("I", 0.0, 0.1), word("am", 0.11, 0.2), word("uh", 0.25, 0.42),
        word("ready", 0.5, 0.8), word("now.", 0.81, 1.0),
    ]
    report = build_report(script, [segment(rows)], 1.5)
    row = report["sentences"][0]
    assert row["status"] == "matched"
    assert row["coverage"] == 1.0
    assert len(row["audio_ranges"]) == 2
    assert report["extra_speech_groups"] == 1


def test_bengali_token_alignment():
    script = "আমি ভালো আছি। আজ কাজ শুরু করি।"
    rows = [word("আমি", 0.0, 0.2), word("ভালো", 0.21, 0.5), word("আছি।", 0.51, 0.7),
            word("আজ", 1.0, 1.2), word("কাজ", 1.21, 1.5), word("শুরু", 1.51, 1.8), word("করি।", 1.81, 2.0)]
    report = build_report(script, [segment(rows)], 2.5)
    assert report["script_sentence_count"] == 2
    assert report["matching_percentage"] == 100.0
    assert report["matched_sentences"] == 2


def test_three_intentional_bengali_repetitions_remain_three_distinct_rows():
    script = "আমি প্রস্তুত। শুরু করি। আমি প্রস্তুত। আবার বলছি, আমি প্রস্তুত।"
    words = [
        word("আমি", 0.0, .1), word("প্রস্তুত।", .11, .4),
        word("শুরু", .7, .9), word("করি।", .91, 1.1),
        word("আমি", 1.4, 1.5), word("প্রস্তুত।", 1.51, 1.8),
        word("আবার", 2.1, 2.3), word("বলছি,", 2.31, 2.5),
        word("আমি", 2.6, 2.7), word("প্রস্তুত।", 2.71, 3.0),
    ]
    report = build_report(script, [segment(words)], 3.2)
    assert report["script_sentence_count"] == 4
    assert report["matched_sentences"] == 4
    assert [row["include"] for row in report["sentences"]] == [True] * 4


def test_low_confidence_partial_line_is_not_auto_selected():
    script = "This sentence is fully recorded."
    words = [word("This", 0, .1), word("sentence", .11, .2)]
    report = build_report(script, [segment(words)], .5)
    row = report["sentences"][0]
    assert row["status"] in {"review", "missing"}
    assert row["include"] is False


def test_audio_quality_measurement_uses_ffmpeg_on_real_wav(tmp_path):
    import math, struct, wave
    from backend.app.audio import analyze_audio_quality
    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
        frames = [int(12000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(16000)]
        out.writeframes(b"".join(struct.pack("<h", x) for x in frames))
    result = analyze_audio_quality(path)
    assert result["mean_volume_db"] is not None
    assert result["peak_volume_db"] is not None
    assert result["near_clipping_samples"] == 0


def test_best_take_candidates_prioritize_complete_sentence():
    script = "I am ready now."
    rows = [
        word("I", 0.0, .1, .98), word("am", .11, .2, .98), word("ready", .21, .4, .98),
        word("I", 1.0, 1.1, .80), word("am", 1.11, 1.2, .80), word("ready", 1.21, 1.4, .80), word("now.", 1.41, 1.6, .80),
    ]
    report = build_report(script, [segment(rows)], 2.0)
    row = report["sentences"][0]
    assert len(row["take_candidates"]) >= 2
    assert row["selected_take"]["complete"] is True
    assert row["start"] >= 1.0


def test_repeated_script_occurrences_are_not_collapsed_by_take_ranking():
    script = "আমি প্রস্তুত। আমি প্রস্তুত। আমি প্রস্তুত।"
    words = [word("আমি",0,.1),word("প্রস্তুত।",.11,.3),word("আমি",.6,.7),word("প্রস্তুত।",.71,.9),word("আমি",1.2,1.3),word("প্রস্তুত।",1.31,1.5)]
    report = build_report(script,[segment(words)],1.8)
    assert len(report["sentences"]) == 3
    assert [r["status"] for r in report["sentences"]] == ["matched"]*3
    assert [r["start"] for r in report["sentences"]] == sorted(r["start"] for r in report["sentences"])


def test_faster_whisper_language_hint_is_forwarded(monkeypatch, tmp_path):
    from backend.app import audio
    class Word:
        word='আমি'; start=0.0; end=.3; probability=.9
    class Segment:
        start=0.0; end=.3; text=' আমি'; avg_logprob=-.1; words=[Word()]
    class Info:
        language='bn'
    class FakeModel:
        def transcribe(self, path, **kwargs):
            assert kwargs['language']=='bn'
            return iter([Segment()]), Info()
    monkeypatch.setattr(audio, '_model', ('faster-whisper', FakeModel()))
    rows, language=audio.transcribe(tmp_path/'unused.wav', 'bn')
    assert language=='bn' and rows[0]['words'][0]['word']=='আমি'
    monkeypatch.setattr(audio, '_model', None)


def test_bengali_combining_marks_stay_inside_orthographic_words():
    from backend.app.matching import tokens
    assert tokens('আজকের পরীক্ষায় আমরা বাংলা কথা শিখছি।') == ['আজকের','পরীক্ষায়','আমরা','বাংলা','কথা','শিখছি']


def test_bengali_asr_spelling_variants_still_align_to_script():
    script='আজকের পরীক্ষায় আমরা বাংলা কথা শিখছি।'
    spoken='আজকের পরিখায় আম্রা বাংগলা কথা শিকছি।'
    rows=[word(w,i*.4,i*.4+.3,.8) for i,w in enumerate(spoken.split())]
    report=build_report(script,[segment(rows)],2.6)
    row=report['sentences'][0]
    assert row['status']=='matched'
    assert row['include'] is True
    assert row['coverage'] > .8
    assert report['matching_percentage'] > 80
    assert report['extra_speech_groups']==0


def test_bengali_script_alignment_handles_realistic_asr_orthography_errors():
    script='আজকের পরীক্ষায় আমরা বাংলা কথার স্বীকৃতি যাচাই করছি। এই বাক্যটি সঠিকভাবে শনাক্ত হওয়া উচিত।'
    spoken='আজকের পরিখায় আম্রা বাংগলা কথা শিকিতি জাচাই করছি এই বাক্কটি সটিক ভাবে সনাক্ত হয়া উচিত'
    rows=[word(w,i*.35,i*.35+.25,.75) for i,w in enumerate(spoken.split())]
    report=build_report(script,[segment(rows)],6.0)
    assert report['matched_sentences']==2
    assert report['missing_sentences']==0
    assert report['matching_percentage'] > 75
    assert report['sentences'][1]['start'] > report['sentences'][0]['end']


def test_repeated_script_lines_keep_their_order_even_when_later_take_is_cleaner():
    script='আমি প্রস্তুত। আমি প্রস্তুত।'
    rows=[word('আমি',0,.1,.45),word('প্রস্তুত।',.11,.4,.45),
          word('আমি',1.0,1.1,.99),word('প্রস্তুত।',1.11,1.4,.99)]
    report=build_report(script,[segment(rows)],1.6)
    assert report['matched_sentences']==2
    assert [r['start'] for r in report['sentences']]==[0,1.0]
    assert [r['include'] for r in report['sentences']]==[True,True]


def test_matching_accepts_actual_large_v3_turbo_bengali_output_fixture():
    script='আজকের পরীক্ষায় আমরা বাংলা কথার স্বীকৃতি যাচাই করছি। এই বাক্যটি সঠিকভাবে শনাক্ত হওয়া উচিত।'
    fixture=[('আজকের',0,.58,.946),('পরিখায়',.58,1.02,.939),('আম্রা',1.02,1.3,.906),('বাংগলা',1.3,1.62,.881),
      ('কথা',1.62,1.9,.977),('শিকিতি',1.9,2.32,.926),('জাচাই',2.32,2.66,.954),('করছি',2.66,3.1,.891),
      ('এই',3.1,4.24,.863),('বাক্কটি',4.24,4.72,.903),('সটিক',4.72,5.04,.940),('ভাবে',5.04,5.34,.901),
      ('সনাক্ত',5.34,5.72,.904),('হয়া',5.72,6.04,.748),('উচিত',6.04,6.38,.896)]
    words=[{'word':w,'start':a,'end':b,'probability':p} for w,a,b,p in fixture]
    report=build_report(script,[{'start':0,'end':6.38,'text':' '.join(x[0] for x in fixture),'words':words}],6.38)
    assert report['matched_sentences']==2 and report['missing_sentences']==0
    assert report['matching_percentage'] > 80
    assert report['extra_speech_groups']==0
