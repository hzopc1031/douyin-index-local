def timestamp(seconds):
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    seconds, millis = divmod(millis, 1000)
    return f'{hours:02}:{minutes:02}:{seconds:02},{millis:03}'


def format_srt(segments):
    return ''.join(f'{index}\n{timestamp(segment.start)} --> {timestamp(segment.end)}\n{segment.text.strip()}\n\n'
                   for index, segment in enumerate(segments, 1))


def transcribe(video, subtitle, model):
    from faster_whisper import WhisperModel
    segments, _ = WhisperModel(model).transcribe(str(video))
    subtitle.write_text(format_srt(segments), encoding='utf-8')
