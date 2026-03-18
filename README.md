<div align="center">

# maplesgedu.com

Official website for **Maple Education (SG枫叶留学)** — a Singapore-based study abroad consultancy.

[www.maplesgedu.com](https://www.maplesgedu.com)

</div>

## About

Maple Education provides study abroad services for students pursuing education in Singapore, including AEIS exam preparation, university applications, international school admissions, and visa assistance.

## Tech Stack

- Static HTML / CSS / JavaScript
- Google Fonts (Inter)
- Hosted on Cloudflare Pages
- Ad monitoring dashboard with ECharts (`/ads`)

## Pages

| Page | Description |
|------|-------------|
| `index.html` | Home — hero, services overview, testimonials |
| `services.html` | Detailed service offerings |
| `about.html` | Company background and team |
| `contact.html` | Contact form and info |
| `ads/` | Internal ad performance dashboard |

## Development

No build step required. Open any `.html` file directly or serve locally:

```bash
python3 -m http.server 8080
```

## Deployment

The site is deployed to Cloudflare Pages. The `CNAME` file points to `www.maplesgedu.com`.

## License

All rights reserved.
