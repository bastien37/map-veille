// MAP – Carte à points.
//
// Module chargé à la demande : tant que le visiteur reste sur la liste ou sur la carte des
// zones, ni Leaflet ni ce fichier ne sont téléchargés (voir chargerCarte() dans index.html).
//
// Deux règles tiennent tout le reste :
//   - on ne demande que ce que la carte montre (bbox + période + thème), jamais la planète ;
//   - un événement = un marqueur, quel que soit le nombre de médias qui en parlent.

const VENDOR = "vendor/";
let L = null;                 // Leaflet, une fois chargé
let carte = null, agregats = null, calqueTuiles = null;
let requete = null;           // AbortController de la requête en cours
let minuterie = null;
let config = null;
let surSelection = null;      // rappel : un événement a été choisi
let derniers = [];            // derniers événements reçus, pour la liste de repli

const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---------------------------------------------------------------------------
// Chargement différé de Leaflet
// ---------------------------------------------------------------------------
function ajouterCss(href) {
  return new Promise((ok, ko) => {
    if (document.querySelector(`link[href="${href}"]`)) return ok();
    const l = document.createElement("link");
    l.rel = "stylesheet"; l.href = href;
    l.onload = ok; l.onerror = () => ko(new Error(href));
    document.head.appendChild(l);
  });
}

function ajouterJs(src) {
  return new Promise((ok, ko) => {
    if (document.querySelector(`script[src="${src}"]`)) return ok();
    const s = document.createElement("script");
    s.src = src; s.defer = true;
    s.onload = ok; s.onerror = () => ko(new Error(src));
    document.head.appendChild(s);
  });
}

async function chargerLeaflet() {
  if (L) return L;
  await Promise.all([ajouterCss(VENDOR + "leaflet.css"),
                     ajouterCss(VENDOR + "MarkerCluster.css"),
                     ajouterCss(VENDOR + "MarkerCluster.Default.css")]);
  await ajouterJs(VENDOR + "leaflet.js");
  await ajouterJs(VENDOR + "leaflet.markercluster.js");
  L = window.L;
  if (!L) throw new Error("Leaflet n'a pas pu être chargé");
  // Leaflet cherche ses icônes à côté de son propre fichier : on le lui dit explicitement,
  // sinon les marqueurs sont invisibles.
  L.Icon.Default.prototype.options.imagePath = VENDOR + "images/";
  return L;
}

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------
export async function creer(conteneur, conf, options = {}) {
  config = conf;
  surSelection = options.surSelection || (() => {});
  await chargerLeaflet();
  if (carte) return carte;

  const vue = conf.vue_initiale || { lat: 25, lon: 10, zoom: 2 };
  carte = L.map(conteneur, {
    center: [vue.lat, vue.lon],
    zoom: vue.zoom,
    minZoom: 2,
    worldCopyJump: true,          // se déplacer au-delà de l'antiméridien reste naturel
    maxBounds: [[-85, -Infinity], [85, Infinity]],
    attributionControl: true,
  });
  calqueTuiles = L.tileLayer(conf.tuiles.url, {
    attribution: conf.tuiles.attribution,
    maxZoom: conf.tuiles.zoom_max || 18,
    crossOrigin: true,
  }).addTo(carte);

  agregats = L.markerClusterGroup({
    maxClusterRadius: conf.rayon_agregat_px || 55,
    showCoverageOnHover: false,
    chunkedLoading: true,
    // nombre de SOURCES et non de marqueurs : c'est ce que le lecteur veut savoir
    iconCreateFunction: g => {
      const n = g.getAllChildMarkers().reduce((s, m) => s + (m.options.nbSources || 1), 0);
      const taille = n < 10 ? "petit" : n < 100 ? "moyen" : "grand";
      return L.divIcon({ html: `<div><span>${n}</span></div>`,
                         className: `agregat agregat-${taille}`,
                         iconSize: L.point(40, 40) });
    },
  });
  carte.addLayer(agregats);

  carte.on("moveend zoomend", () => planifier());
  // les commandes de zoom doivent être atteignables au clavier et annoncées
  carte.getContainer().setAttribute("role", "application");
  carte.getContainer().setAttribute("aria-label",
    "Carte des événements. Utilisez les touches fléchées pour vous déplacer, " +
    "plus et moins pour zoomer, Tabulation pour atteindre les marqueurs.");
  return carte;
}

// ---------------------------------------------------------------------------
// Chargement des points
// ---------------------------------------------------------------------------
function planifier() {
  clearTimeout(minuterie);
  minuterie = setTimeout(() => rafraichir(), 250);   // un déplacement = une requête, pas dix
}

export function invalider() {
  if (carte) carte.invalidateSize();
}

let filtres = { theme: null, jours: 30 };

export function appliquerFiltres(nouveaux) {
  filtres = { ...filtres, ...nouveaux };
  if (carte) rafraichir();
}

function bbox() {
  const b = carte.getBounds();
  const borne = (v, m) => Math.max(-m, Math.min(m, v));
  return [borne(b.getWest(), 180), borne(b.getSouth(), 90),
          borne(b.getEast(), 180), borne(b.getNorth(), 90)].map(v => v.toFixed(4)).join(",");
}

export async function rafraichir() {
  if (!carte) return;
  if (requete) requete.abort();            // une vue plus récente a été demandée
  requete = new AbortController();
  const params = new URLSearchParams({ bbox: bbox(), jours: filtres.jours,
                                       limite: config.points_max || 600 });
  if (filtres.theme) params.set("theme", filtres.theme);
  let d;
  try {
    const r = await fetch(`api/evenements?${params}`, { signal: requete.signal });
    if (!r.ok) throw new Error("HTTP " + r.status);
    d = await r.json();
  } catch (e) {
    if (e.name !== "AbortError") annoncer("Le serveur n'a pas répondu pour cette zone.");
    return;
  }
  derniers = d.evenements;
  dessiner(d.evenements);
  const plafond = config.points_max || 600;
  annoncer(d.total >= plafond
    ? `${d.total} points affichés, les plus suivis d'abord. Zoomez pour voir le détail.`
    : `${d.total} événement${d.total > 1 ? "s" : ""} dans la zone affichée.`);
}

function dessiner(evenements) {
  agregats.clearLayers();
  const marqueurs = evenements.map(e => {
    const m = L.marker([e.lat, e.lon], {
      nbSources: e.nb_sources,
      keyboard: true,
      title: `${e.titre} — ${e.lieu || ""} (${e.nb_sources} source${e.nb_sources > 1 ? "s" : ""})`,
      alt: e.titre,
    });
    m.on("click keypress", ev => {
      if (ev.type === "keypress" && ev.originalEvent.key !== "Enter") return;
      surSelection(e.id);
    });
    return m;
  });
  agregats.addLayers(marqueurs);
}

function annoncer(texte) {
  const z = $("#carte-etat");
  if (z) z.textContent = texte;
}

// ---------------------------------------------------------------------------
// Panneau des sources d'un événement
// ---------------------------------------------------------------------------
const dateCourte = d => new Date(d).toLocaleString("fr-FR",
  { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

export async function chargerEvenement(id) {
  const r = await fetch(`api/evenements/${encodeURIComponent(id)}`);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

export function htmlPanneau(e) {
  const lignes = e.sources.map(s => `
    <li>
      ${s.nouveau ? '<span class="badge">Nouveau</span>' : ""}
      <h4>${esc(s.titre)}</h4>
      <div class="meta">
        <span>${esc(s.source)}</span><span>${dateCourte(s.date)}</span>
        <a href="${esc(s.lien)}" target="_blank" rel="noopener noreferrer">Lire sur le site du média ↗</a>
      </div>
    </li>`).join("");
  return `
    <div class="panneau-tete">
      <h3>${esc(e.lieu || e.titre)}</h3>
      <p>${e.nb_sources} source${e.nb_sources > 1 ? "s" : ""} ·
         ${dateCourte(e.date_debut)}${e.date_fin !== e.date_debut ? " → " + dateCourte(e.date_fin) : ""}</p>
      <button class="chip" id="fermer-panneau">Fermer</button>
    </div>
    <ol class="sources">${lignes}</ol>`;
}

// Liste de repli : exactement les mêmes données que la carte, sans la carte.
// C'est l'alternative accessible, et le secours si les tuiles ne chargent pas.
export function htmlListe() {
  if (!derniers.length) return '<li class="empty">Aucun événement localisé dans cette zone.</li>';
  return derniers.map(e => `
    <li>
      <h3>${esc(e.titre)}</h3>
      <div class="meta">
        <span>${esc(e.lieu || "lieu inconnu")}</span>
        <span>${e.nb_sources} source${e.nb_sources > 1 ? "s" : ""}</span>
        <span>${dateCourte(e.date_fin)}</span>
        <button class="lien-evenement" data-evt="${e.id}">Voir les sources</button>
      </div>
    </li>`).join("");
}

export function evenements() {
  return derniers;
}
