# ShipMatch QA — Session 06: cross-cutting checks (4 Oct 2026)

Same rules: real-user testing in the in-app browser, **issues listed, nothing fixed**. App on `http://localhost:8001` (Chromium only; the built-in browser is the only engine available).
Method: DOM-level audits run over all 44 pages, rendered colour-contrast scan, same-origin frames at 375/768/1100 px for responsive checks, real Tab/Enter/Escape key presses for keyboard checks, 600 generated shipments (Northwind sandbox) for volume.
Starting DB backup: `%TEMP%\db.sqlite3.bak-session06-start`. Issue ids continue from session 05 (last was QA-052).

## 1. Coverage map

| Area | Status | Result |
|---|---|---|
| HTTP security headers | ✅ | CSP (`default-src 'self'`, no inline scripts), X-Frame-Options, nosniff, Referrer-Policy, Permissions-Policy, COOP, `Cache-Control: no-store` on pages. |
| HTML structure / a11y audit, 44 pages | ✅ | No unlabelled inputs, no images without alt, no duplicate ids, no nameless buttons/links, landmarks and `lang` present, CSRF on every POST form, no inline handlers. Findings: QA-062. |
| Colour contrast (rendered, 8 key pages) | ✅ | Mostly passes; muted text slightly under AA (QA-058). |
| Keyboard-only | ✅ | Skip link first; logical tab order; every focused element has a 2 px blue ring; `<details>` menus open with Enter and close with Escape returning focus; shortcuts dialog is modal, labelled, traps focus and returns it. Minor: QA-060. |
| Screen-reader semantics | 🟡 | Structure is good; flash messages announced via `role=status` (QA-053). Not tested with a real screen reader. |
| Responsive 375 px (44 pages) | ✅ | Only the shipment detail page overflows sideways (QA-006, now pinned down). |
| Responsive 768 px (15 key pages) | ✅ | No overflow. |
| Long / non-Latin / emoji / HTML-like file names | ✅ | Documents list, document page and queue stay within width at 375/768; names render escaped. |
| Dark mode / forced colours | ✅ | Not supported (QA-059). |
| Print stylesheet, reduced motion | ✅ | Both present. |
| Performance at volume (603 shipments in one org) | ✅ | Queue 80–110 ms, search 50–70 ms, dashboard 240 ms, exports 260 ms, 25 rows/page with pager. **`/my-work/` ≈ 2 s** (QA-056). |
| Pagination edge cases | ✅ | `page=0`, `-1` silently show the last page (QA-064). |
| Console / network errors | ✅ | No JavaScript errors and no failed assets on 6 pages checked. |
| Failure handling: bad/missing CSRF, session ended mid-form | ✅ | See QA-054, QA-055. |
| Other browsers (Firefox, Safari), real devices | ⬜ | Not available. |
| Offline / slow network / retry behaviour | ⬜ | Not tested (no network throttling tool). |
| Real screen reader (NVDA/VoiceOver), 400 % zoom, OS text size | ⬜ | Not tested. |
| Concurrency (two users on one shipment) | ⬜ | Not tested. |

## 2. New issues

**QA-054 — Medium — CSRF failures show Django's default, unbranded page**
A bad or missing token returns a bare "Forbidden (403) CSRF verification failed. Request aborted. Help Reason given for failure: CSRF token from POST has incorrect length…" with no navigation. A person whose page went stale (long-open tab, cookie expiry) sees technical text. Add a friendly "Your page expired, reload and try again" view.

**QA-055 — Medium — After the session expires mid-action, sign-in sends you to a blank page**
Submitting a form on a stale page after sign-out redirects to `/account/login/?next=/work/shipments/42/assign/`. That URL only accepts POST, so after signing in the user lands on an **empty 405 page** (no title, no navigation). `next` should only keep GET-able URLs (or fall back to the page the form was on).

**QA-056 — Medium — `/my-work/` takes about 2 seconds**
With 110 assigned/approvable items the page took 1.9–2.1 s on every request (all pages of it), while every other page (queue, search, dashboard) is 50–250 ms. That's roughly 18 ms per row, which points to a per-row query. Re-measure on PostgreSQL; the cost will grow with team size.

**QA-053 — Low — Error messages use `role="status"`**
All flash messages (including errors) are rendered in `<ul class="flash" role="status">`. Errors would be better as `role="alert"` so they interrupt, and a status region added only at page load may not be announced at all.

**QA-057 — Low — Shipment page eagerly loads pdf.js and the first PDF**
Each shipment page downloads `pdf.min.mjs` (506 KB) and `pdf.worker.min.mjs` (1.3 MB) and the first PDF, even where the viewer is stacked at the bottom of the page (see QA-007). The first PDF request is aborted and repeated. Consider loading on first "Show PDF".

**QA-058 — Low — Muted text is just under WCAG AA**
`#64748b` on `#f1f3f6` is **4.28:1** (needs 4.5:1) for small labels such as "Period ending", counts in tabs, "selected", and descriptions list on shipment pages; table headers (`#64748b` on `#f7f8fa`) are 4.48:1. Darkening the grey a notch fixes all of them. (Disabled primary buttons are exempt.)

**QA-059 — Low — No dark mode or forced-colours support; font sizes are in px**
No `prefers-color-scheme` or `forced-colors` rules, so the app stays light and ignores OS high-contrast mode. The stylesheet has 121 `font-size: …px` declarations and one `rem`, so the browser's default text-size setting doesn't scale the UI (page zoom does).

**QA-060 — Low — Skip link doesn't move focus**
Activating "Skip to content" scrolls to `#main` but `<main>` has no `tabindex="-1"`, so focus stays on `<body>`. Chromium handles this via the sequential-focus starting point; some browsers don't. Re-check in Safari/Firefox.

**QA-061 — Low — Small touch/click targets**
Inline links in tables are 16–20 px tall (shipment pages, statements), native checkboxes are 16 px (webhook events 14 per page, assignment, settings). WCAG 2.2's minimum is 24 px. Shipment page has about 10 undersized targets.

**QA-062 — Low — Heading structure and page titles**
Heading levels skip on `/disputes/` and `/notifications/` (h1→h3) and on shipment/statement pages (h2→h4). Several data tables on shipment pages (the extracted-value tables) have no header cells. `<title>` is identical for filtered views (`Review queue – ShipMatch` for every tab/filter), and the blank 405 page has no title.

**QA-063 — Info — Static files carry no cache headers or compression in this setup**
`app.css` (90 KB), fonts, pdf.js are served with no `Cache-Control` and no `Content-Encoding`. Fine for `runserver`, but confirm the production proxy (Caddy/WhiteNoise) adds long-lived caching and gzip/brotli.

**QA-064 — Low — `?page=0` and negative pages show the last page**
`/review/?status=all&page=0` → "601–603 of 603" (last page), not page 1 or an error. Out-of-range numbers likely behave the same.

**Pinned down (QA-006):** at 375 px the shipment page overflows because of three elements: the landed-cost "spread by" radio group (`fieldset.plain-fieldset > .lc-bases > label.check`, +40 px), the field-legend row in document cards (`.legend`, +41–43 px) and the shared-invoice split table (`table.lc-split`).

## 3. Worked well
- Security headers and CSP are strict; no inline scripts anywhere.
- Semantic HTML is excellent: every control labelled, landmarks, `lang`, skip link, no duplicate ids.
- Full keyboard operation with obvious focus, native `<details>` menus with Escape handling, accessible modal dialog.
- Layout holds from 375 px up, including with very long, RTL, CJK and emoji file names.
- Speed with 600+ shipments is good (queue/search ~100 ms) apart from `/my-work/`.
- No JavaScript or asset errors. Print and reduced-motion styles exist.
- Session-expiry redirect works (apart from QA-055).

## 4. State left behind
- Northwind sandbox: 600 shipments created for the volume test and **deleted** (the global shipment counter moved up to ~SHP-000671). Six more unreadable test documents (long/RTL/CJK/emoji/HTML-like names) remain in Northwind's document list. Acme data unchanged. Browser left signed in as Northwind owner.

## 5. Suggested next steps
1. Fix-and-retest passes: re-run this session's audit scripts after fixes (they are quick to repeat).
2. Real devices / other browsers (Safari iOS, Firefox) and a screen-reader pass (NVDA + VoiceOver) on the review queue and shipment page.
3. Throttled/offline network, large PDFs (50 MB+) and long-running uploads, concurrent edits by two users.
4. Run on PostgreSQL with production settings (`DEBUG=False`): error pages (404/500 styling), static caching, QA-051 and QA-056.
