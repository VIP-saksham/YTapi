# 🚀 KeyaraAPI Website → Cloudflare Pages (FREE)

Repo private karne ke baad bhi website chalani hai? Cloudflare Pages se deploy karo.
Free plan pe private repo bhi chalega, unlimited bandwidth, aur GitHub se zyada fast (global CDN).

**Naya URL hoga:** `https://keyaraapi.pages.dev` (ya jo naam deploy me doge)

---

## Option A — Drag & Drop (sabse aasan, 2 minute) ⭐ RECOMMENDED

1. VPS pe site zip ready hai: `/root/keyaraapi-site.zip`
   (nagee kholo to `_site/` folder milega)
2. Browser me kholo: **https://dash.cloudflare.com** → login karo
3. Left menu me **Workers & Pages** → **Create application** → **Pages** tab
4. **Upload assets** pe click karo
5. Project name: `keyaraapi` → **Create project**
6. Zip ko drag-drop karo (ya "select from computer" se `_site` folder ke andar ki `index.html` upload karo)
7. **Deploy site** → done! 🎉
8. Tumhara link: `https://keyaraapi.pages.dev`

## Option B — VPS se wrangler deploy (API token se, repeatable)

1. Cloudflare dashboard → right-top **profile icon** → **My Profile** → **API Tokens** → **Create Token**
2. Template: **"Cloudflare Pages — Edit"** select karo → Continue → Create
3. Token mujhe do (ya khud VPS pe `export CLOUDFLARE_API_TOKEN=...` karke):
   ```bash
   cd /root/nakshu/keyaraapi
   bash deploy_cloudflare.sh
   ```
4. Har website update ke baad bas ye script chalani hai. Done!

## Option C — GitHub se auto-deploy (best long-term)

1. Cloudflare dashboard → **Workers & Pages** → **Create** → **Pages** → **Connect to Git**
2. GitHub authorize karo → repo `VIP-saksham/api` select karo (PRIVATE bhi chalega ✅)
3. Build settings:
   - Framework preset: **None**
   - Build command: _(khali chhod do)_
   - Build output directory: `_site`
4. **Save and Deploy** → ab har push pe site khud update hogi 🎉

---

## Iske baad (main karta hu)

- [ ] `tgbot.py` ka website link naye URL pe update
- [ ] GitHub `pages.yml` workflow hata dena (ab zarurat nahi)
- [ ] Repo private kar sakte ho — website Cloudflare se chalti rahegi ✅

> Zero downtime: GitHub Pages tab tak chalta rahega jab tak Cloudflare live nahi ho jata.
