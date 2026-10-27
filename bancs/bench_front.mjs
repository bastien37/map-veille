import { createRequire } from 'module';
const require = createRequire('/opt/node-tools/');
const { chromium } = require('playwright');

const URL = process.argv[2] || 'http://127.0.0.1:8940/';
const LATENCE_CDN = Number(process.argv[3] ?? 0);

const nav = await chromium.launch();
const page = await (await nav.newContext()).newPage();
const reqs = [];

await page.route(u => !u.href.startsWith('http://127.0.0.1'), async route => {
  if (LATENCE_CDN) await new Promise(r => setTimeout(r, LATENCE_CDN));
  reqs.push({ url: route.request().url(), taille: 0, externe: true });
  await route.abort().catch(() => {});
});

page.on('response', async rep => {
  const u = rep.url();
  if (!u.startsWith('http://127.0.0.1')) return;
  let n = 0;
  try { n = (await rep.body()).length; } catch {}
  reqs.push({ url: u, taille: n, statut: rep.status(), enc: rep.headers()['content-encoding'] || '-' });
});

// chauffe : premier chargement ecarte (demarrage du serveur, caches froids)
await page.goto(URL, { waitUntil: 'load' }).catch(() => {});
await page.waitForTimeout(400);
reqs.length = 0;
const t0 = Date.now();
await page.goto(URL, { waitUntil: 'load' });
await page.waitForFunction(
  () => { const l = document.querySelectorAll('#list li');
          return l.length > 1 && !l[0].textContent.includes('Chargement'); },
  { timeout: 30000 }).catch(() => {});
const tPret = Date.now() - t0;
await page.waitForTimeout(300);

const m = await page.evaluate(() => {
  const n = performance.getEntriesByType('navigation')[0] || {};
  const fcp = performance.getEntriesByType('paint').find(p => p.name === 'first-contentful-paint');
  // encodedBodySize = octets reellement transferes (apres gzip) ; decodedBodySize = apres
  // decompression. C'est le premier qui compte pour le temps de chargement.
  const r = performance.getEntriesByType('resource').concat([n]);
  const transfere = r.reduce((s, e) => s + (e.encodedBodySize || 0), 0);
  const decode = r.reduce((s, e) => s + (e.decodedBodySize || 0), 0);
  return { dcl: Math.round(n.domContentLoadedEventEnd), load: Math.round(n.loadEventEnd),
           fcp: fcp ? Math.round(fcp.startTime) : null, transfere, decode,
           articles: document.querySelectorAll('#list li').length,
           noeuds: document.getElementsByTagName('*').length };
});

const local = reqs.filter(r => !r.externe);
const octets = local.reduce((s, r) => s + r.taille, 0);
console.log(`\n--- Front ${URL}  (latence CDN simulee : ${LATENCE_CDN} ms) ---`);
console.log(`requetes          : ${reqs.length} (${local.length} locales, ${reqs.length - local.length} externes)`);
console.log(`octets transferes : ${(m.transfere / 1024).toFixed(1)} KiB `
          + `(${(m.decode / 1024).toFixed(1)} KiB une fois decompresses)`);
console.log(`first paint (FCP) : ${m.fcp} ms`);
console.log(`DOMContentLoaded  : ${m.dcl} ms`);
console.log(`load              : ${m.load} ms`);
console.log(`fil affiche       : ${tPret} ms  (${m.articles} lignes, ${m.noeuds} noeuds DOM)`);
for (const r of reqs)
  console.log(`   ${r.externe ? 'EXT' : '   '} ${String((r.taille / 1024).toFixed(1)).padStart(7)}k  enc=${r.enc || '-'}  ${r.url.slice(0, 80)}`);
await nav.close();
