# Frontend maintenance

- Read `../docs/design-system.md` before changing the admin UI.
- Use React, TypeScript and Ant Design. Reuse shared business components; do not add a second component library or a generic admin template.
- Keep colors, typography, sizing and component defaults in `src/theme.ts`. Prefer Ant Design defaults and public props before adding any style. Use `src/styles.css` for layout and domain-specific visuals only; Markdown/Milkdown adaptation belongs to `src/editor.css`.
- Do not target `.ant-*` internals or add `!important`. Use documented semantic `classNames`/`styles` slots when a layout needs a component container; do not recreate component appearance through inline overrides.
- Reuse `NavigationSider` / `NavigationMenu` and the shared page scroll boundaries. Do not add page-specific sidebar, table density or collapsed-menu patches.
- Keep UI copy concise. Avoid decorative subtitles, explanatory banners on every page, and non-functional controls.
- Preserve backend permission boundaries, one-time Key disclosure, token-in-memory sessions and lease ownership checks. Render knowledge and submitted text as data, never HTML.
- Include loading, empty, error and unavailable states. Confirm consequential revoke/disable/unbind actions and show failures without closing the dialog.
- Folder pickers use single-click selection and double-click navigation, with an explicit Open action for keyboard users.
- Run `npm test` and `npm run build` after functional changes. Verify affected flows in a browser using isolated data, never mutate live knowledge or grants for testing.
- Source lives here; generated assets in `../folder_kb/web` are ignored. Do not edit them directly or commit node_modules, secrets, build products or local state.
