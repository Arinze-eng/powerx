---
name: video-editing
description: Edit video and audio — Cloudinary first for trims, crops, resizes, transcodes, joins and poster frames, with local ffmpeg in the sandbox for what Cloudinary cannot do (probe, contact sheets, transcription, captions, shorts, blurred-background verticals, downloads, background mattes). Use whenever the user asks to edit, cut, crop, resize, upscale, caption, transcribe, download or clip a video or its audio.
metadata: {"nanobot":{"emoji":"🎬","os":["linux"],"always":false}}
---

# Video editing in the sandbox

## Route the edit first — Cloudinary, then local ffmpeg

Before a single local ffmpeg command, decide which tool owns the operation:

- **`cloudinary_video_edit` — first choice.** Trims, crops and re-frames, resizes,
  transcodes and format changes, joins and poster frames. It stores the clip once
  in Cloudinary and renders the transformation on delivery. Try this before
  `media_sandbox` every time, for a user's own footage as much as anything else.
- **`media_sandbox` — for what Cloudinary does not offer:** `probe`, `watch`,
  `frames`, `transcribe`, `captions`, `shorts`, `blur`, `download`, `bg`, and any
  filter or codec Cloudinary refuses. It is also the fallback when a Cloudinary
  render fails
  and the user still needs the result.

Being in a sandbox is not a reason to edit locally. A local ffmpeg encode of
something Cloudinary renders is the wrong first move even when it would work.

The workflow below is `media_sandbox`'s. For these actions the work happens inside
the user's execution sandbox with local ffmpeg: no upload of their footage to a
third party, and nothing encoded on the application host.

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

Only run an action below after the routing rule at the top: `trim`, `crop`,
`scale`/`hd`, `concat` and (on accounts with the add-on) generative animation
belong to `cloudinary_video_edit` first, so reach for this table for
`media_sandbox`'s own actions and as the fallback.

| Intent | Action |
| --- | --- |
| Cut a range out | `trim` (add `fast=true` for an instant keyframe-aligned cut) |
| Make it vertical / square | `crop` with `aspect="9:16"`, `focus="face"` for a speaker |
| Vertical WITHOUT losing the sides | `blur` — the whole frame, blurred, behind a portrait clip |
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

**`blur` is the vertical you want for anything that is not a talking head.** `crop`
throws the sides of a 16:9 frame away, which is wrong for a screen share, a two-shot
or a wide stage: the viewer loses the content. `blur` keeps the entire frame — it
scales it to COVER the portrait canvas, blurs it (and dims it slightly), and lays
the untouched frame on top at `foreground` of the output width (default 0.92). This
is the shape short-form platforms reward. It takes `start`/`duration` in the SAME
pass, so a one-minute vertical out of a two-hour recording is ONE encode, not a
trim to an intermediate file and a second encode of it:

```
media_sandbox(action="blur", input="/path/long.mp4", start="00:20:00",
              duration="60", aspect="9:16", output_height=1920)   # one encode
```

Set `blur=0` to keep the background sharp, `dim=0` to stop it darkening, and
`foreground=1.0` for the tightest crop of the blurred border. The result reports
`background_visible=false` when the source is already as tall as the target, so a
plain bordered video is never mistaken for a composite.

**Crop focus.** `focus="face"` samples five frames, finds the speaker with OpenCV
and keeps them centred. It is deliberately best-effort: if no face is found it falls
back to the anchor (centre by default) and says so. A screen recording wants
`anchor="center"`, not `focus="face"`.

**HD is never faked.** `scale`/`hd` refuse to downscale unless you pass
`allow_downscale`, and they never invent resolution: a 720p source cannot become a
real 1080x1920, so `shorts` from it come out 404x718 — exactly 9:16 — with
`upscaled=false`. Say that plainly to the user instead of implying a 1080p master.

**Long work is detached.** These actions routinely outlive one sandbox command:
`hd`, `scale`, `concat`, `transcribe`, `captions`, `bg`, `download`, `shorts`,
`blur`. The
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

Then deliver the files. They live in the sandbox, so fetch the finished one out with
the sandbox tool's own `download_url` action and give the user **the onlyfiles.com link
it returns** — never a sandbox path, a preview/signed URL or a `/f/` deployment link.

## Downloading from YouTube (and anywhere else yt-dlp reaches)

You can pull a video straight off a link and then edit it — all inside the sandbox,
with nothing running on the application host. This is the normal way to start a
short/clip job from a URL the user pasted.

```
media_sandbox(action="download", url="https://youtu.be/9lY-1J8LPS0",
              out="/home/ubuntu/dl/clip.mp4", quality="720")
```

**`out` decides the shape of the result — read this, it is the #1 thing to get right:**

* ends in a media extension (`clip.mp4`) → it is the **output file**, and the video
  lands at exactly that path.
* anything else (`/home/ubuntu/dl`) → it is a **directory**, and the file is named
  from the video's own title.

Prefer the **file** form whenever you are going to edit the result: every later
action needs the real path, and a title-named file inside a folder is a path you
have to go and read back out of the result before you can use it.

`codec="h264"` is the default and is what you want: H.264/AAC re-encodes fast on a
CPU sandbox and is accepted by TikTok, Instagram and every editor. `codec="best"`
takes the platform's raw preference instead — YouTube now serves AV1/Opus, which is
slower to decode and refused by several uploaders — so only use it for a file that
will never be edited or re-uploaded.

`subtitles="en"` also pulls the platform's own captions — cheaper and more accurate
than transcribing when they exist. One URL means one video: `playlist=true` is
required for a whole channel, deliberately.

### The link → edit chain

Download, then **watch it** before editing — a downloaded video's shape is rarely
what the user described. The full job, in order:

```
media_sandbox(action="download", url="<link>", out="/home/ubuntu/dl/clip.mp4",
              quality="720")                       # 1. fetch
media_sandbox(action="watch", input="/home/ubuntu/dl/clip.mp4",
              count=16, timestamps=True)           # 2. LOOK at it
media_sandbox(action="shorts", input="/home/ubuntu/dl/clip.mp4",
              count=3, captions=True)              # 3. or trim/crop/hd
```

Then fetch the finished file out with `download_url` and give the user the
`onlyfiles.com` link (see "Delivering the result" above). Never tell the user to
download it themselves and never hand them a raw ffmpeg command.

**Where this runs.** The download, the ffmpeg encode and the model weights all live
in the user's execution sandbox. The application host only issues the command and
reads the JSON back — it never runs yt-dlp, ffmpeg or a whisper model. Keep it that
way: if you find yourself writing a local `ffmpeg`/`yt-dlp` invocation, you have
taken the wrong path.

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

## What Cloudinary owns, and where local ffmpeg stands

`cloudinary_video_edit` is the first tool for `trim`, `crop`, `transcode`,
`poster` (a still frame), `concat` and, where the account has the add-on,
generative `animate`. It stores the clip once and renders the edit on delivery.

Stay on `media_sandbox` for the actions Cloudinary has no equivalent for (`probe`,
`watch`, `frames`, `transcribe`, `captions`, `shorts`, `download`, `bg`), and fall
back to it when a Cloudinary render fails and the user still needs the result —
saying in the reply that you fell back and why. Do not silently present a local
ffmpeg edit as the hosted result.

A clip that only exists inside the sandbox can still go to Cloudinary: publish it
with `{"action":"download_url","path":"<path in the sandbox>"}` and pass the
returned `https://onlyfiles.com/…` link as `source` (or `second_clip`). A link the
user attached goes straight in, unwrapped.
