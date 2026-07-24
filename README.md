# Oficina Web

Site estático com duas origens de conteúdo: artigos escritos de raiz e uma secção
de agregação que liga a artigos publicados noutros sites.

Gerador em Python 3.9+, **sem dependências**. Não é preciso instalar nada.

## Correr

```bash
python3 build.py            # build completo, vai buscar os feeds
python3 build.py --offline  # usa a cache, não acede à rede
python3 build.py --serve    # build + servidor em http://localhost:8000
```

O resultado fica em `dist/`. É uma pasta de ficheiros estáticos — pode ser servida
por qualquer coisa.

## Estrutura

```
config.json          nome do site, URL, lista de feeds
content/posts/*.md   your own articles
content/pages/*.md   standalone pages (about, contact...)
static/              copiado tal e qual para dist/
.cache/feeds.json    última leitura dos feeds (rede de segurança)
dist/                gerado — não editar à mão
```

## Escrever um artigo

Criar um ficheiro em `content/posts/`:

```markdown
---
title: O título do artigo
description: Uma frase até 155 caracteres. Aparece no Google e nas partilhas.
date: 2026-07-24
updated: 2026-07-24
tags: uma, duas, três
slug: url-do-artigo
---

O texto em markdown. Suporta títulos, listas, citações, blocos de código,
**negrito**, *itálico*, `código` e [links](https://exemplo.pt).
```

Os `##` de segundo nível entram automaticamente no índice lateral, a partir de
três secções.

## Mudar de tema

O motor não sabe nada sobre o assunto do site. Para o reaproveitar:

1. Editar `config.json` — nome, tagline, descrição, URL, feeds.
2. Substituir os ficheiros em `content/posts/`.
3. Ajustar as duas cores de marca no topo de `static/style.css`.

## Publicar

`dist/` é estático puro. Qualquer um destes serve, sem alterações:

- **Cloudflare Pages** — comando de build `python3 build.py`, diretório `dist`.
- **Netlify** — igual.
- **GitHub Pages** — correr o build numa action e publicar `dist`.
- **Alojamento próprio** — copiar `dist/` para a pasta pública.

Antes de publicar, mudar `base_url` no `config.json` para o domínio real. Todos os
`canonical`, o sitemap e o feed derivam daí.

---

## Decisões de SEO já tomadas

O que o gerador faz sozinho, e porquê.

**Cada página tem `<title>` e `meta description` próprios.** O título segue o padrão
`Assunto — Nome do site`, com o mais distintivo à esquerda.

**`<link rel="canonical">` em todas as páginas.** Evita conteúdo duplicado por
variações de URL.

**JSON-LD.** `WebSite` e `Organization` no início; `BlogPosting` com datas, autor e
contagem de palavras em cada artigo; `BreadcrumbList` para a navegação.

**Open Graph e Twitter Cards** em todas as páginas, com uma imagem de partilha em SVG
gerada a partir do nome do site.

**Sitemap XML** com `lastmod` real, tirado da data de atualização de cada artigo.
Páginas `noindex` ficam de fora.

**Feed RSS próprio** em `/feed.xml`, anunciado no `<head>`.

**HTML semântico:** um `<h1>` por página, `<article>`, `<time datetime>`, link de
salto para o conteúdo, `lang` correto, foco de teclado visível, `prefers-reduced-motion`
respeitado.

**Sem JavaScript.** Todo o conteúdo está no HTML servido. Nada depende de execução
no cliente.

### A decisão menos óbvia: `/radar/` é `noindex`

A página de agregação está marcada com `noindex, follow`, excluída do sitemap e
bloqueada no `robots.txt`.

É deliberado. A agregação é conteúdo que não é nosso, muda todos os dias e existe em
dezenas de sítios — o perfil exato do que um motor de busca classifica como baixo
valor. Nunca ia posicionar-se, porque compete com a fonte original e perde. O que
podia fazer era arrastar consigo os artigos que **têm** hipótese.

O `follow` mantém os links a passar valor para quem estamos a citar, que é o justo.

O Radar continua a servir o que serve bem: dar a quem já conhece o site uma razão
para voltar.

### Como as fontes são tratadas

- Só título, resumo curto e link. O texto completo fica na origem.
- Nome da fonte visível em cada item.
- Links de saída com `rel="nofollow noopener external"`.
- Os feeds são lidos no build, não a cada visita.
- `User-Agent` identifica o site e o seu URL.
- Se um feed não responder, o build usa a última cache e continua.

### O que falta fazer à mão

1. **`base_url` real** no `config.json`.
2. **Google Search Console** — registar o domínio e submeter o sitemap.
3. **Lighthouse** — correr uma vez depois de publicar (F12 → Lighthouse no Chrome).
4. **Imagem de partilha** — o `og.svg` funciona, mas algumas redes sociais preferem
   PNG. Se importar, exportar 1200×630 e trocar as referências no `build.py`.
5. **Escrever mais.** É a parte que nenhuma configuração substitui.
