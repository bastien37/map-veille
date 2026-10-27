// Comparatif Go : meme travail que le collecteur Python (telecharger, parser, classer)
// sur les memes 95 flux locaux. Bibliotheque standard uniquement.
package main

import (
	"encoding/json"
	"encoding/xml"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"
)

type Item struct {
	Title       string   `xml:"title"`
	Link        string   `xml:"link"`
	PubDate     string   `xml:"pubDate"`
	Description string   `xml:"description"`
	Category    []string `xml:"category"`
}
type Flux struct {
	Items []Item `xml:"channel>item"`
}

var nonAlnum = regexp.MustCompile(`[^a-z0-9]+`)

func normaliser(s string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(s) {
		switch r {
		case 'á', 'à', 'â', 'ä', 'ã':
			r = 'a'
		case 'é', 'è', 'ê', 'ë':
			r = 'e'
		case 'í', 'ì', 'î', 'ï':
			r = 'i'
		case 'ó', 'ò', 'ô', 'ö', 'õ':
			r = 'o'
		case 'ú', 'ù', 'û', 'ü':
			r = 'u'
		case 'ç':
			r = 'c'
		case '\'', '’':
			r = ' '
		}
		if r < unicode.MaxASCII {
			b.WriteRune(r)
		} else {
			b.WriteRune(' ')
		}
	}
	return " " + strings.TrimSpace(nonAlnum.ReplaceAllString(b.String(), " ")) + " "
}

type Automate struct {
	exact   map[string][]string
	prefixe map[string][]string
	expr    map[string][]string
	nmax    int
	lmin    int
	lmax    int
}

func compiler(regles map[string][]string) *Automate {
	a := &Automate{exact: map[string][]string{}, prefixe: map[string][]string{},
		expr: map[string][]string{}, nmax: 1, lmin: 99}
	for cat, mots := range regles {
		for _, mot := range mots {
			m := strings.TrimSpace(normaliser(mot))
			if m == "" {
				continue
			}
			if strings.Contains(m, " ") {
				n := strings.Count(m, " ") + 1
				if n > a.nmax {
					a.nmax = n
				}
				a.expr[strconv.Itoa(n)+"|"+m] = append(a.expr[strconv.Itoa(n)+"|"+m], cat)
			} else if strings.HasSuffix(mot, "*") {
				a.prefixe[m] = append(a.prefixe[m], cat)
				if len(m) < a.lmin {
					a.lmin = len(m)
				}
				if len(m) > a.lmax {
					a.lmax = len(m)
				}
			} else {
				a.exact[m] = append(a.exact[m], cat)
			}
		}
	}
	return a
}

func (a *Automate) balayer(texte string, poids int, scores map[string]int, prem map[string]int, note bool) {
	jetons := strings.Fields(texte)
	pos := 0
	for i, j := range jetons {
		pos += 1
		add := func(cat string) {
			scores[cat] += poids
			if note {
				if p, ok := prem[cat]; !ok || pos < p {
					prem[cat] = pos
				}
			}
		}
		for _, c := range a.exact[j] {
			add(c)
		}
		for k := a.lmin; k <= a.lmax && k <= len(j); k++ {
			for _, c := range a.prefixe[j[:k]] {
				add(c)
			}
		}
		for n := 2; n <= a.nmax && i+n <= len(jetons); n++ {
			for _, c := range a.expr[strconv.Itoa(n)+"|"+strings.Join(jetons[i:i+n], " ")] {
				add(c)
			}
		}
		pos += len(j)
	}
}

func (a *Automate) classer(titre, resume string) string {
	scores, prem := map[string]int{}, map[string]int{}
	a.balayer(normaliser(titre), 3, scores, prem, true)
	a.balayer(normaliser(resume), 1, scores, prem, false)
	if len(scores) == 0 {
		return ""
	}
	cats := make([]string, 0, len(scores))
	for c := range scores {
		cats = append(cats, c)
	}
	sort.Slice(cats, func(i, j int) bool {
		if scores[cats[i]] != scores[cats[j]] {
			return scores[cats[i]] > scores[cats[j]]
		}
		return prem[cats[i]] < prem[cats[j]]
	})
	return cats[0]
}

func main() {
	port := os.Args[1]
	par, _ := strconv.Atoi(os.Args[2]) // nombre de telechargements simultanes
	var regles map[string]interface{}
	raw, _ := os.ReadFile("../../collecteur/regles.json")
	json.Unmarshal(raw, &regles)
	conv := func(k string) map[string][]string {
		out := map[string][]string{}
		for cat, v := range regles[k].(map[string]interface{}) {
			for _, m := range v.([]interface{}) {
				out[cat] = append(out[cat], m.(string))
			}
		}
		return out
	}
	autoZones, autoThemes := compiler(conv("zones")), compiler(conv("themes"))

	lents := map[int]bool{7: true, 23: true, 61: true, 13: true, 47: true}
	var urls []string
	for i := 0; i < 100; i++ {
		if !lents[i] {
			urls = append(urls, fmt.Sprintf("http://127.0.0.1:%s/feed/%d", port, i))
		}
	}

	client := &http.Client{Timeout: 30 * time.Second,
		Transport: &http.Transport{MaxIdleConnsPerHost: par, Proxy: nil}}

	t0 := time.Now()
	type res struct {
		body []byte
	}
	out := make([][]byte, len(urls))
	sem := make(chan struct{}, par)
	var wg sync.WaitGroup
	var octets int64
	var mu sync.Mutex
	for i, u := range urls {
		wg.Add(1)
		go func(i int, u string) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			r, err := client.Get(u)
			if err != nil {
				return
			}
			defer r.Body.Close()
			b, _ := io.ReadAll(io.LimitReader(r.Body, 5<<20))
			out[i] = b
			mu.Lock()
			octets += int64(len(b))
			mu.Unlock()
		}(i, u)
	}
	wg.Wait()
	tDl := time.Since(t0)

	t1 := time.Now()
	var items []Item
	for _, b := range out {
		var f Flux
		if xml.Unmarshal(b, &f) == nil {
			items = append(items, f.Items...)
		}
	}
	tParse := time.Since(t1)

	t2 := time.Now()
	nz, nt := 0, 0
	for _, it := range items {
		resume := it.Description + " " + strings.Join(it.Category, " ")
		if autoZones.classer(it.Title, resume) != "" {
			nz++
		}
		if autoThemes.classer(it.Title, resume) != "" {
			nt++
		}
	}
	tTri := time.Since(t2)

	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	fmt.Printf("Go : %d flux (%d simultanes), %d Kio, %d articles\n", len(urls), par, octets/1024, len(items))
	fmt.Printf("  telechargement %7.2f s\n  parsing XML    %7.2f s\n  tri mots-cles  %7.2f s\n  TOTAL          %7.2f s\n",
		tDl.Seconds(), tParse.Seconds(), tTri.Seconds(), (tDl + tParse + tTri).Seconds())
	fmt.Printf("  memoire max    %7.1f Mio   (zones classees %d, themes %d)\n",
		float64(ms.Sys)/1024/1024, nz, nt)
}
