# Att göra i workern

## 1. Städa temp-katalogen även vid krasch (funnen 6 sep 2026)

Efter ungefär tolv timmars drifttid börjar workers falla på:

```
OSError: [Errno 28] No space left on device: '/tmp/tmpXXXXXXXX'
CalledProcessError: Command '['ffmpeg', '-y', '-loglevel', 'error', '-i', '/tmp/.../audio.bin', ...]'
```

ffmpeg-felen är följdfel — omkodningen misslyckas för att disken redan är full.

**Orsak:** varje jobb skapar en temp-katalog med `audio.bin` plus den omkodade
16 kHz-versionen. Katalogen städas vid normal utgång men blir kvar när ett jobb
kraschar eller avbryts, och en långlivad worker samlar därför på sig filer tills
containerdisken tar slut.

**Fix:** lägg omkodning och transkribering i `try/finally` med
`shutil.rmtree(tmpdir, ignore_errors=True)` i `finally`, alternativt
`tempfile.TemporaryDirectory()` som kontexthanterare runt hela jobbet.
Överväg också en uppstartsstädning som tömmer `/tmp/tmp*` när workern startar —
en ny worker ska aldrig ärva skräp från en tidigare körning på samma värd.

**Tillfällig lindring redan gjord:** containerdisken höjd från 20 till 40 GB
(endpoint-inställning, inte kod). Det skjuter problemet framåt men löser det inte.

**Drabbade 6 sep:** tio inspelningar, återställda till kön för hand med
`update recordings set transcribe_status = 'ny' where transcribe_status = 'fel'`.

## 2. Registry-autentisering

Hämtningen av `ghcr.io/increvendi/kb-whisper-worker:latest` fastnade i "pending"
i elva timmar 5 sep när nya workers skulle startas, sannolikt anonym
hämtningsgräns hos ghcr.io. Lägg in GitHub-token i RunPods
container registry credentials så att hämtningen sker inloggat.
