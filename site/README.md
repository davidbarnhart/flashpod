# flashpod landing page

A single-page marketing site for [flashpod](https://github.com/davidbarnhart/flashpod).
Pure HTML + CSS — **no build step, no dependencies.**

```
site/
├── index.html      # the page
├── styles.css      # all styling
└── assets/
    ├── favicon.svg
    ├── README.md   # what images to drop in
    └── screenshot-*.png   # (add your own)
```

## Preview locally

Just open `index.html` in a browser, or serve the folder:

```sh
cd site
python3 -m http.server 8000   # then visit http://localhost:8000
```

## Deploy to a static host

This is a plain static folder, so any host works. Point the host at the `site/`
directory as the publish/output directory:

- **Netlify** — drag-and-drop the `site/` folder, or connect the repo and set
  the publish directory to `site`. No build command.
- **Cloudflare Pages** — connect the repo; build command: *(none)*; build output
  directory: `site`.
- **Vercel** — import the repo; framework preset "Other"; output directory
  `site`.
- **GitHub Pages** — set Pages to serve from this branch and the `/site` folder,
  or move these files to `/docs`.

## Custom domain

Once you've bought the domain (e.g. `flashpod.dev`), add it in your host's
dashboard and follow its DNS instructions. If you go with GitHub Pages, also add
a `CNAME` file in `site/` containing just the bare domain, e.g.:

```
flashpod.dev
```

The page's Open Graph URL in `index.html` is currently set to
`https://flashpod.dev/` — update it if you pick a different domain.
