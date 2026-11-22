# Bug Fixes

Fixes places in the original game where the description and the actual effect do not match, plus a few broken visual / appearance assets. No new content, and your saves are not modified.

## Fixed (8 packages)

1. **Parry (chainsword and other melee weapons)**
   The description says it absorbs 80% of damage, but the original only absorbed 20%. It now works as described.

2. **Grav Gun / Flamer / Hand Flamer**
   The weapon models appeared as grey checkerboard placeholders (materials failed to load). They now display correctly.

3. **Dark Angels / Black Templars chapter icons**
   The two chapter badges were swapped in the UI. Each is now back in the right place.

4. **Alacrity (Black Templars passive)**
   The description says "25% chance to gain +1 AP at the start of your turn"; it never triggered. It now triggers correctly. Applied as two independent packages: the missing `TurnsLeft` guard fix and the `STAT_Plus1AP` AP-per-turn component.

5. **Imperial Fists Praetorian (passive)**
   The description says "20% damage reduction", but the original granted a damage bonus instead. It now grants real damage reduction.

6. **Crippling Shot**
   The description said 1% while the actual effect was 2%. Both the Chinese and English descriptions now say 2%.

7. **Imperial Fists Mastercrafted Heavy Bolter appearance**
   The Steam build has zero emissive strength on this weapon while the PC EE build glows. The emissive parameter is now matched to PC EE; the mesh and textures are unchanged.

## Notes

- Fixes only touch game data and text; your saves are not modified and existing progress is unaffected.
- Uninstalling restores every file to the original version.
- Apart from the appearance fix, every change makes the game match what it already tells the player; nothing is rebalanced on top of that.
