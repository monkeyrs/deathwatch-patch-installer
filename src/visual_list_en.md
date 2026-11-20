# Visual Enhancement · Three-Tier Parameters

The installer changes one game configuration file. Your original file is
backed up first and can be restored at any time.
The three tiers = Base + tier extras; vanilla is not a tier here — use the
installer's Restore mode.

## [Base] shared by Integrated / Discrete / Max

· 16x anisotropic filtering — clearer ground and textures in top-down view
· One step sharper texture detail — less blur at a distance
· Motion blur off — no smearing when the camera moves
· Depth of field off — distant units stay sharp
· Chromatic aberration off — no purple/green fringing at screen edges
· Tonemapper and auto exposure at their highest settings
· Vsync off, frame cap 144 — frame rate follows your graphics card
· Frame-rate smoothing off — no 30/60 FPS clamp
· Asset-loading budgets 10 / 30 — units and effects appear sooner when
  entering battles or switching maps

## [Integrated GPU] low-power devices

· Entry-level shadows (1024 resolution, 2 cascades)
· Basic bloom and lens flare
· Brighter image

## [Discrete GPU] default tier (GTX 1050 Ti class)

· 150% supersampling — finer image
· High-quality shadows (2048 resolution, 3 cascades)
· Anti-aliasing and ambient occlusion at max — smoother edges, grounded units
· Bloom, lens flare and screen-space reflections at max
· Sharper translucency lighting — glowing spores, blood and plasma tracers
· Brighter image

## [Max] RTX 4060-class mainstream GPUs

· 200% supersampling
· MSAA 8x
· Shadows at 4096 resolution, 4 cascades
· Wider ambient occlusion radius
· One more step of texture detail

## [FAQ]

· No visible change? In-game graphics settings may override these values;
  see the visual_enhance README next to the game folder for details.
· Frame drops or glitches? Try a lower tier. Max was verified on real hardware,
  but it is heavy on the graphics card; Discrete is the recommended tier.
· This enhancement never touches the exe or any program file.
