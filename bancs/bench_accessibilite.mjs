// Contrôles d'accessibilité et de mise en page mesurés dans un vrai navigateur :
// contrastes (clair et sombre), navigation au clavier, structure, et largeur téléphone.
//
//   node bancs/bench_accessibilite.mjs http://127.0.0.1:8000/
import { createRequire } from 'module';
const require = createRequire('/opt/node-tools/');
const { chromium } = require('playwright');

const URL = process.argv[2] || 'http://127.0.0.1:8000/';
const nav = await chromium.launch();
let echecs = 0;
const verdict = (ok, texte) => { if (!ok) echecs++; console.log(`  ${ok ? 'ok ' : 'NON'} ${texte}`); };

for (const theme of ['light', 'dark']) {
  const page = await (await nav.newContext({
    viewport: { width: 1280, height: 900 }, colorScheme: theme })).newPage();
  await page.route('**tile.openstreetmap.org**', r => r.abort());
  await page.goto(URL, { waitUntil: 'load' });
  await page.waitForTimeout(900);

  const contrastes = await page.evaluate(() => {
    const lum = c => {
      const [r, g, b] = c.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number)
        .map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
                              return (x + 0.05) / (y + 0.05) };
    const fond = e => { let n = e; while (n) {
        const c = getComputedStyle(n).backgroundColor;
        if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') return c; n = n.parentElement }
      return 'rgb(255,255,255)' };
    const cas = [
      ['titre h1', 'h1'], ['sous-titre', 'h1 span'], ['date de collecte', '.status'],
      ['titre d\'article', '#list li h3'], ['métadonnées', '#list .meta span'],
      ['lien article', '#list .meta a'], ['puce de thème', '#themes .chip'],
      ['puce active', '#vues .chip[aria-pressed="true"]'], ['pied de page', 'footer'],
      ['état de la carte', '#carte-etat'], ['badge Nouveau', '.badge'],
      ['récap, titre', '#recap summary h2'], ['récap, thème', '#recap h3'],
      ['récap, texte', '#recap p'], ['récap, signature', '#recap .signature'],
    ];
    return cas.map(([nom, sel]) => {
      const e = document.querySelector(sel);
      if (!e) return { nom, absent: true };
      const s = getComputedStyle(e);
      return { nom, ratio: +ratio(s.color, fond(e)).toFixed(2),
               taille: parseFloat(s.fontSize), gras: +s.fontWeight >= 700 };
    }).filter(c => !c.absent);
  });

  console.log(`\n--- contrastes, thème ${theme} (WCAG AA : 4,5 ; 3,0 si grand texte) ---`);
  for (const c of contrastes) {
    const grand = c.taille >= 24 || (c.gras && c.taille >= 18.66);
    verdict(c.ratio >= (grand ? 3 : 4.5),
            `${c.nom.padEnd(20)} ${String(c.ratio).padStart(5)} : 1   (${c.taille}px${grand ? ', grand' : ''})`);
  }
  await page.context().close();
}

// --- clavier et structure
const page = await (await nav.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
await page.route('**tile.openstreetmap.org**', r => r.abort());
await page.goto(URL, { waitUntil: 'load' });
await page.waitForTimeout(1200);

console.log('\n--- navigation au clavier ---');
// on tabule jusqu'à atteindre la carte plutôt qu'un nombre fixe de fois : chaque thème
// ajouté en met deux de plus devant elle, et un plafond figé finirait par la rater.
const ordre = [];
for (let i = 0; i < 60 && !ordre.includes('carte-points'); i++) {
  await page.keyboard.press('Tab');
  ordre.push(await page.evaluate(() => {
    const e = document.activeElement;
    return e === document.body ? 'body'
      : (e.id || e.textContent?.trim().slice(0, 24) || e.tagName).replace(/\s+/g, ' ');
  }));
}
console.log('  ' + ordre.slice(0, 16).join(' → ')
            + (ordre.length > 16 ? ` → … (${ordre.length} arrêts en tout)` : ''));
verdict(ordre.some(o => /Carte|Liste seule/.test(o)), 'les boutons de vue sont atteignables');
verdict(ordre.some(o => /jours|24 h/.test(o)), 'les boutons de période sont atteignables');
verdict(ordre.includes('carte-points'), 'la carte est atteignable');
verdict(new Set(ordre).size > 6, 'l\'ordre de tabulation parcourt la page');

const visible = await page.evaluate(() => {
  document.querySelector('#themes .chip').focus();
  const s = getComputedStyle(document.activeElement, ':focus-visible');
  return s.outlineStyle !== 'none' || s.outlineWidth !== '0px';
});
verdict(visible, 'le focus est visible');

console.log('\n--- structure ---');
const struct = await page.evaluate(() => ({
  lang: document.documentElement.lang,
  titre: document.title,
  h1: document.querySelectorAll('h1').length,
  sautH: (() => { const n = [...document.querySelectorAll('h1,h2,h3,h4')].map(e => +e.tagName[1]);
                  return n.every((v, i) => i === 0 || v - n[i - 1] <= 1) })(),
  aria: document.querySelectorAll('[aria-label],[aria-pressed],[aria-live],[role]').length,
  boutonsSansNom: [...document.querySelectorAll('button')]
    .filter(b => !b.textContent.trim() && !b.getAttribute('aria-label')).length,
  imagesSansAlt: [...document.querySelectorAll('img')].filter(i => !i.hasAttribute('alt')).length,
}));
verdict(struct.lang === 'fr', `langue déclarée : ${struct.lang}`);
verdict(struct.h1 === 1, `un seul h1 (${struct.h1})`);
verdict(struct.sautH, 'pas de saut dans les niveaux de titre');
verdict(struct.boutonsSansNom === 0, `tous les boutons ont un nom (${struct.boutonsSansNom} sans)`);
verdict(struct.imagesSansAlt === 0, `toutes les images ont un alt (${struct.imagesSansAlt} sans)`);
verdict(struct.aria > 10, `attributs ARIA posés : ${struct.aria}`);

console.log('\n--- téléphone (375 x 667) ---');
await page.setViewportSize({ width: 375, height: 667 });
await page.waitForTimeout(700);
const tel = await page.evaluate(() => ({
  debord: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  carte: Math.round(document.querySelector('#carte-points').getBoundingClientRect().width),
  colonnes: getComputedStyle(document.querySelector('.grid')).gridTemplateColumns.split(' ').length,
  cibles: [...document.querySelectorAll('button, a')]
    .filter(e => e.offsetParent)
    .filter(e => { const r = e.getBoundingClientRect(); return r.height > 0 && r.height < 24 }).length,
}));
verdict(tel.debord <= 0, `pas de débordement horizontal (${tel.debord}px)`);
verdict(tel.colonnes === 1, `une seule colonne (${tel.colonnes})`);
verdict(tel.carte > 300 && tel.carte <= 375, `la carte tient dans l'écran (${tel.carte}px)`);
console.log(`  i   ${tel.cibles} cibles tactiles de moins de 24px de haut`);

console.log(`\n${echecs ? echecs + ' contrôle(s) en échec' : 'tous les contrôles passent'}`);
await nav.close();
process.exit(echecs ? 1 : 0);
