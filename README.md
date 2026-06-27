# flashpod.dev

The landing page for [flashpod](https://github.com/davidbarnhart/flashpod) — a
command-line tool for putting flash storage cards into early-generation iPods.

Pure HTML + CSS. **No build step, no dependencies.** Served via GitHub Pages at
[flashpod.dev](https://flashpod.dev).

```
.
├── index.html      # the page
├── styles.css      # all styling
├── CNAME           # custom domain for GitHub Pages (flashpod.dev)
├── .nojekyll       # tell Pages to serve files as-is (skip Jekyll)
└── assets/
    ├── favicon.svg
    ├── README.md   # which screenshots to drop in
    └── screenshot-*.png   # (add your own)
```

## Preview locally

Open `index.html` in a browser, or serve the folder:

```sh
python3 -m http.server 8000   # then visit http://localhost:8000
```

## Deploy (GitHub Pages)

1. Push this repo to GitHub.
2. **Settings → Pages →** set *Source* to "Deploy from a branch", branch `main`,
   folder `/ (root)`.
3. The `CNAME` file already requests `flashpod.dev` — set up DNS (below).

## Custom domain DNS

At your domain registrar, point `flashpod.dev` at GitHub Pages:

| Type  | Name | Value |
|-------|------|-------|
| A     | `@`  | `185.199.108.153` |
| A     | `@`  | `185.199.109.153` |
| A     | `@`  | `185.199.110.153` |
| A     | `@`  | `185.199.111.153` |
| CNAME | `www`| `davidbarnhart.github.io` |

Then in **Settings → Pages**, enter `flashpod.dev` as the custom domain and
enable *Enforce HTTPS* (it appears once the certificate is issued — can take a
few minutes to an hour).

> Verify the current GitHub Pages IPs in their docs before relying on the table
> above — they change rarely, but check.

## Screenshots

The three tiles reference `assets/screenshot-flash.png`, `assets/screenshot-add.png`,
and `assets/screenshot-ipod.png`. Drop real images in with those names and the
page picks them up automatically. See `assets/README.md` for sizes.
