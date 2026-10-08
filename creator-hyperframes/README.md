# Creator HyperFrames renderer

The Python entry point is `app.services.creator.hyperframes.render_visual(payload, log, progress=None)`.
It builds an escaped, offline HTML composition from prepared SRT captions and the user's title.
Original subtitle phrases can appear as animated point cards; it does not invent selling points.

Install the pinned local dependencies with `npm ci --prefix creator-hyperframes`.
HyperFrames `0.8.137` and GSAP `3.15.0` are pinned in the lockfile.
The renderer uses the local FFmpeg executable and an existing Chromium headless shell, selected
through `HYPERFRAMES_BROWSER_PATH`, `MPT_RENDER_BROWSER`, or the Playwright browser cache.
It does not download a browser, font, template, agent Skill, or cloud model during generation.

The genuine HyperFrames CLI creates a transparent VP9 WebM overlay for short renders or
a streaming ProRes 4444 MOV overlay for longer renders. FFmpeg composites that alpha layer
onto the existing silent base video, preserving the full source timeline without looping it.
VP9 is explicitly decoded with `libvpx-vp9`, which retains alpha.
Audio is mixed once by the existing Python rendering service after this entry point returns.

Successful rendering retains the HTML project and `hyperframes-render.json` for diagnosis,
and removes the potentially large alpha intermediate. Set `MPT_HYPERFRAMES_KEEP_OVERLAY=1`
for alpha-channel inspection. Failed renders retain their intermediate and log.
No Remotion fallback is used.

Official references: [Rendering](https://hyperframes.heygen.com/guides/rendering),
[CLI](https://hyperframes.heygen.com/packages/cli),
[Determinism](https://hyperframes.heygen.com/concepts/determinism).
