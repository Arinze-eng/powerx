---
name: video-editing
description: Edit video and audio inside the sandbox with local ffmpeg — watch a clip, cut, crop to vertical, put it in HD, remove a background, download from a link, transcribe, burn captions, and pull publishable shorts out of a long recording. Use whenever the user asks to edit, cut, crop, resize, upscale, caption, transcribe, download or clip a video or its audio.
metadata: {"nanobot":{"emoji":"🎬","os":["linux"],"always":false}}
---

# Video editing in the sandbox

Everything video the user asks for runs through the `media_sandbox` tool. The work
happens inside their execution sandbox with local ffmpeg: no paid API, no upload of
their footage to a third party, and nothing encoded on the application host.

The workflow is always the same four moves. Skipping the first two is what produces
bad edits.

## 1. Probe — know the media before touching it

```
media_sandbox(action="probe", input="/path/clip.mp4")
```

Gives duration, resolution, fps, frame count, codecs and stream layout. Do this
first whenever you are about to cut, crop or scale: the crop maths, the short
windows and the "is this already HD" decision all depend on it.

## 2. Watch — look at the video

```
media_sandbox(action="watch", input="/path/clip.mp4", count=12, timestamps=True,
              out="/path/sheet.png")
```

One contact-sheet PNG with twelve tiled stills, each stamped with its timestamp —
**read that image**. Numbers tell you the shape of a video; only the sheet tells you
where the face is, which section is dead air, and which moment is worth clipping.
Use `count=16..24` for a long recording. `frames` gives individual full-size stills
when you need to inspect one moment closely.

## 3. Edit — one action per intent

| Intent | Action |
| --- | --- |
| Cut a range out | `trim` (add `fast=true` for an instant keyframe-aligned cut) |
| Make it vertical / square | `crop` with `aspect="9:16"`, `focus="face"` for a speaker |
| "Put it in HD" | `hd` (defaults to 1080) |
| Explicit resize | `scale` with `height` or `width` |
| Join clips | `concat` with `inputs` |
| Soundtrack only | `audio` with `format="mp3"` |
| Remove/replace a background | `bg` |
| Download a link | `download` with `url` |
| Words | `transcribe` |
| Burn the words in | `captions` |
| Clips worth posting | `shorts` |

Quality is the default: H.264 CRF 18 preset slow, `+faststart`, AAC 192k. Use
`preset="veryfast"` or `crf=23` only for a draft you intend to redo.

**Crop focus.** `focus="face"` samples five frames, finds the speaker with OpenCV
and keeps them centred. It is deliberately best-effort: if no face is found it falls
back to the anchor (centre by default) and says so. A screen recording wants
`anchor="center"`, not `focus="face"`.

**HD is never faked.** `scale`/`hd` refuse to downscale unless you pass
`allow_downscale`, and they never invent resolution: a 720p source cannot become a
real 1080x1920, so `shorts` from it come out 404x718 — exactly 9:16 — with
`upscaled=false`. Say that plainly to the user instead of implying a 1080p master.

**Long work is detached.** These actions routinely outlive one sandbox command:
`hd`, `scale`, `concat`, `transcribe`, `captions`, `bg`, `download`, `shorts`. The
tool waits ~200 s inline, then returns a `job_id`:

```
media_sandbox(action="job", job_id="<id>", wait=120)
```

Poll that id — **never re-run the edit**, because a second run is a second encode
competing for the same CPU. Every action returns verified JSON: the artifact's own
duration, resolution, streams and byte size, probed after the write. A 0-byte or
wrong-shaped file fails loudly rather than shipping.

## 4. Verify — before you claim it worked

Read the result, do not assume it:

* `output_probe` — the real shape of what was written.
* `captions_ok` / `caption_warnings` — ffmpeg exits 0 and draws **no** captions when
  the font cannot be resolved, so the video looks perfect and has no captions in it.
  If `captions_ok` is false, fix the font (re-run `install`) before telling the user
  the captions are burned in.
* `mode` on a trim — `copy` means keyframe-aligned, so its duration is honestly a
  second or two off; `encode` is frame-exact.
* `upscaled` on `shorts`/`hd`.
* `plan_shorts`' `reason` on each clip — it names the words-per-second and hook
  score a clip was chosen for, so you can justify the pick.

Then deliver the files. They live in the sandbox, so give the paths and fetch one
out with the sandbox tool's own `download_url` action when the user wants to keep it.

## Downloading

```
media_sandbox(action="download", url="https://youtu.be/...", out="/path",
              quality="1080")            # or audio_only=true, section="*00:01:00-00:02:30"
```

`subtitles="en"` also pulls the platform's own captions — cheaper and more accurate
than transcribing when they exist. One URL means one video: `playlist=true` is
required for a whole channel, deliberately.

Download, then **watch it** before editing. A downloaded video's shape is rarely
what the user described.

## Transcripts and captions

```
media_sandbox(action="transcribe", input="/path/clip.mp4", format="srt",
              model="base", language="auto")
media_sandbox(action="captions", input="/path/clip.mp4", style="shorts")
```

`transcribe` writes `.srt`, `.json`, `.vtt` or `.txt`, and caches the result beside
the media as `<name>.transcript.json` — `captions` and `shorts` reuse that cache, so
transcribe once per file. Style `shorts` is big, bold and sits above the bottom UI
chrome; `clean` is a normal lower-third; `karaoke` is a coloured emphasis look.

## Shorts from a long recording

```
media_sandbox(action="shorts", input="/path/long.mp4", count=3,
              min_seconds=15, max_seconds=45, captions=True)
```

It transcribes if needed, scores windows on words-per-second and hook words and
gives a sentence-boundary bonus so a clip never starts mid-word, then picks
non-overlapping windows greedily by score. Each clip gets a vertical crop (face
aware), an optional caption burn, a thumbnail `.jpg`, a matching `.srt`, and the
whole set is described by `manifest.json`.

Tighten `min_seconds`/`max_seconds` rather than raising `count` when the plan keeps
picking the same good section: the planner refuses to overlap clips, so asking for
more than the recording has just returns fewer.

## Background removal

```
media_sandbox(action="bg", input="/path/photo.png", out="/path/cutout.png")
media_sandbox(action="bg", input="/path/clip.mp4", out="/path/cutout.webm")
media_sandbox(action="bg", input="/path/clip.mp4", background="FFFFFF",
              out="/path/on_white.mp4")
```

A still gives a PNG with alpha. A video is matted per frame: no `background` gives a
transparent `.webm` (VP9 with alpha), and `background="RRGGBB"` gives an opaque
`.mp4` with the original audio re-attached. `model="u2net_human_seg"` is the right
model for a person; the default `u2net` is general purpose. This is the slowest
action there is — one pass per frame — so always route it through `job`.

## First run in a fresh sandbox

`ffmpeg`, `yt-dlp`, `faster-whisper` and `rembg` are not in the base image. The
first media action in a new sandbox must be:

```
media_sandbox(action="install")
```

It fetches and installs the chain (~3-12 min: static ffmpeg, yt-dlp, OpenCV, the
whisper and rembg weights) and **waits for it**. If it reports `stage="installing"`,
the install is progressing normally: poll `action="status"` until `ready=true`, then
run the action you wanted. Never hand the user ffmpeg commands to run locally, and
never tell them to check back later — the waiting is the tool's job.
