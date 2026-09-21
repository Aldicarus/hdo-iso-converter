"""Mide el contraste REAL de la pantalla, no el que dice el CSS.

No es un test: es el arnés que usan `test_contraste_del_tema` y el estudio
del modo oscuro. Monta las pantallas con `test_las_tres_lenguas_en_pantalla`
—las MISMAS 39, para que no haya dos listas que se desincronicen— y en vez
de leer su texto lee, de cada nodo con texto:

  * el color efectivo de la letra, con su alfa y la opacidad heredada ya
    compuestas (la app apaga cosas con `--op-*` en 104 sitios, y eso ES
    pérdida de contraste aunque el color declarado sea negro);
  * el fondo efectivo, subiendo por los ancestros hasta encontrar opacidad,
    componiendo las capas translúcidas que haya por el camino;
  * la razón de contraste WCAG 2.1 y el mínimo que le toca por tamaño.

Dos aproximaciones conocidas, iguales en los dos temas —así el error
sistemático se cancela al comparar claro contra oscuro, que es para lo que
sirve esto—: la opacidad de grupo se modela como alfa sobre el fondo del
nodo, y los degradados se leen por su primer color.
"""
from __future__ import annotations

# ── El extractor, en JS: sustituye a `leer()` dentro del arnés ────────────
#
# Va como cadena porque corre DENTRO de Chrome, sobre el DOM ya montado:
# `getComputedStyle` es la única fuente que conoce la cascada, el `:hover`
# resuelto, las `var()` y lo que una `var()` inexistente tiró por el camino.
EXTRACTOR = r"""
window.__LEER = (function () {
  const num = s => {
    const m = String(s).match(/-?[\d.]+/g);
    return m ? m.map(Number) : null;
  };
  // `getComputedStyle` devuelve siempre rgb()/rgba(), nunca un hex ni un
  // nombre, así que con esto basta. Un degradado no es un color: se lee su
  // primer rgb(), que es lo que hay detrás del texto en la práctica.
  const color = s => {
    if (!s || s === 'transparent') return null;
    const v = num(s);
    if (!v || v.length < 3) return null;
    return {r: v[0], g: v[1], b: v[2], a: v.length > 3 ? v[3] : 1};
  };
  const primerColorDeFondo = cs => {
    const c = color(cs.backgroundColor);
    if (c && c.a > 0) return c;
    const img = cs.backgroundImage || '';
    if (img && img !== 'none') {
      const m = img.match(/rgba?\([^)]*\)/);
      if (m) return color(m[0]);
    }
    return null;
  };
  const sobre = (fondo, frente, a) => ({
    r: frente.r * a + fondo.r * (1 - a),
    g: frente.g * a + fondo.g * (1 - a),
    b: frente.b * a + fondo.b * (1 - a),
  });
  // WCAG 2.1 §1.4.3 — luminancia relativa.
  const lum = c => {
    const f = v => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const razon = (a, b) => {
    const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
    return (x + 0.05) / (y + 0.05);
  };

  const fondoDe = el => {
    // Las capas, de la más cercana al texto hacia fuera. La opacidad del
    // nodo se aplica a su propio fondo: es la aproximación de la opacidad
    // de grupo, y yerra hacia «peor contraste», que es el lado seguro.
    const capas = [];
    let n = el;
    while (n && n.nodeType === 1) {
      const cs = getComputedStyle(n);
      const c = primerColorDeFondo(cs);
      const op = parseFloat(cs.opacity);
      if (c) capas.push({c, a: c.a * (isNaN(op) ? 1 : op)});
      if (c && c.a >= 1 && op >= 1) break;      // opaco: lo de detrás no se ve
      n = n.parentElement;
    }
    let out = {r: 255, g: 255, b: 255};          // el lienzo del navegador
    for (let i = capas.length - 1; i >= 0; i--) out = sobre(out, capas[i].c, capas[i].a);
    return out;
  };

  const opacidadHeredada = el => {
    let o = 1, n = el;
    while (n && n.nodeType === 1) {
      const v = parseFloat(getComputedStyle(n).opacity);
      if (!isNaN(v)) o *= v;
      n = n.parentElement;
    }
    return o;
  };

  // Sólo los nodos con texto PROPIO: si se midieran también los contenedores
  // se contaría la misma frase tantas veces como ancestros tenga, y el peor
  // caso quedaría enterrado en el promedio.
  //
  // Un control de formulario es la EXCEPCIÓN: su valor no es un nodo de
  // texto sino una propiedad, así que sin este caso `input`, `select` y
  // `textarea` son invisibles para la sonda — y ahí estaba
  // `input.cmv40-lookup-input { color: #000000 }`, negro sobre oscuro, que
  // reportó el usuario. Se mide el valor si lo hay y el placeholder si no,
  // porque un campo vacío es lo normal y su placeholder es lo único que se
  // lee.
  const CONTROL = {INPUT: 1, TEXTAREA: 1, SELECT: 1};
  const textoPropio = el => {
    if (CONTROL[el.tagName]) {
      return (el.value || el.getAttribute('placeholder') || '·').trim();
    }
    let t = '';
    for (const n of el.childNodes) if (n.nodeType === 3) t += n.nodeValue;
    return t.trim();
  };

  // Un borde que se vuelve invisible al cambiar de tema no sale en la
  // medición de texto y sí se nota: separa cajas. Se mira sólo el que ya
  // era visible en el otro tema — muchos son decorativos a propósito.
  const bordeDe = (el, cs, bg) => {
    const anchos = ['Top', 'Right', 'Bottom', 'Left']
      .map(l => parseFloat(cs['border' + l + 'Width']) || 0);
    if (!anchos.some(w => w > 0)) return null;
    const lado = ['Top', 'Right', 'Bottom', 'Left'][anchos.findIndex(w => w > 0)];
    const c = color(cs['border' + lado + 'Color']);
    if (!c || c.a <= 0.01) return null;
    const visto = sobre(bg, c, c.a * opacidadHeredada(el));
    return Math.round(razon(visto, bg) * 100) / 100;
  };

  return function (raiz) {
    const nodos = [];
    const bordes = [];
    for (const el of [raiz, ...raiz.querySelectorAll('*')]) {
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      const bg = fondoDe(el);
      const r = bordeDe(el, cs, bg);
      if (r === null) continue;
      bordes.push({
        sel: (el.tagName.toLowerCase() +
              (el.className && typeof el.className === 'string'
                 ? '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.')
                 : '')).slice(0, 70),
        r,
      });
    }
    const todos = [raiz, ...raiz.querySelectorAll('*')];
    for (const el of todos) {
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      const txt = textoPropio(el);
      if (!txt) continue;
      const fg = color(cs.color);
      if (!fg) continue;
      const bg = fondoDe(el);
      const px = parseFloat(cs.fontSize) || 0;
      const peso = parseInt(cs.fontWeight, 10) || 400;
      // WCAG: «texto grande» es >=24px, o >=18.66px en negrita.
      const grande = px >= 24 || (px >= 18.66 && peso >= 700);
      const alfa = fg.a * opacidadHeredada(el);
      if (alfa <= 0.01) continue;                // invisible: no es contraste
      const letra = sobre(bg, fg, alfa);
      // El placeholder lleva SU color, así que puede desaparecer aunque el
      // valor se lea. Se mide aparte cuando lo hay.
      if (CONTROL[el.tagName] && el.getAttribute('placeholder')) {
        const ph = color(getComputedStyle(el, '::placeholder').color);
        if (ph && ph.a > 0.01) {
          const vistoPh = sobre(bg, ph, ph.a * opacidadHeredada(el));
          nodos.push({
            txt: el.getAttribute('placeholder').slice(0, 40),
            sel: el.tagName.toLowerCase() + '::placeholder',
            px, peso, grande,
            fg: [Math.round(vistoPh.r), Math.round(vistoPh.g), Math.round(vistoPh.b)],
            bg: [Math.round(bg.r), Math.round(bg.g), Math.round(bg.b)],
            r: Math.round(razon(vistoPh, bg) * 100) / 100,
            // Un placeholder es texto secundario por definición: el listón
            // es el de UI, no el de prosa.
            min: 3,
          });
        }
      }
      nodos.push({
        txt: txt.slice(0, 60),
        sel: (el.tagName.toLowerCase() +
              (el.className && typeof el.className === 'string'
                 ? '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.')
                 : '')).slice(0, 70),
        px, peso, grande,
        fg: [Math.round(letra.r), Math.round(letra.g), Math.round(letra.b)],
        bg: [Math.round(bg.r), Math.round(bg.g), Math.round(bg.b)],
        r: Math.round(razon(letra, bg) * 100) / 100,
        min: grande ? 3 : 4.5,
      });
    }
    return {nodos, bordes};
  };
})();
"""


# Las animaciones de entrada arrancan en `opacity: 0`, y un trozo de marcado
# inyectado con `innerHTML` en un `<div>` suelto no siempre las completa antes
# de que Chrome haga el volcado. El resultado es que la opacidad HEREDADA vale
# 0 y la sonda descarta el nodo entero por invisible — **23 de las 76
# pantallas medían cero** y el guard pasaba en verde vigilando el vacío, que
# es el fallo que este repo ya ha tenido cuatro veces.
#
# Apagarlas devuelve los estilos base. No toca ningún color: `animation: none`
# revierte a la declaración de la regla, y ninguna paleta vive en un keyframe.
SIN_ANIMACION = (
    "(function(){var e=document.createElement('style');"
    "e.textContent='*,*::before,*::after{animation:none!important;"
    "transition:none!important}';document.head.appendChild(e);})();"
)


def previo(tema: str) -> str:
    """El JS que corre antes de montar: fija el tema y pone el extractor."""
    fijar = ""
    if tema:
        fijar = f"document.documentElement.dataset.tema = {tema!r};"
    return fijar + SIN_ANIMACION + EXTRACTOR


def medir(tema: str = "") -> dict:
    """{pantalla: [nodo, ...]} con el contraste de cada nodo con texto."""
    from test_las_tres_lenguas_en_pantalla import _pintar
    salida = _pintar("es", previo(tema))
    return {nombre: datos.get("nodos", [])
            for nombre, datos in salida.get("pantallas", {}).items()}


def medir_bordes(tema: str = "") -> dict:
    """{pantalla: [{sel, r}, ...]} con el contraste de cada borde visible."""
    from test_las_tres_lenguas_en_pantalla import _pintar
    salida = _pintar("es", previo(tema))
    return {nombre: datos.get("bordes", [])
            for nombre, datos in salida.get("pantallas", {}).items()}


def fallos(medicion: dict, umbral: float | None = None) -> list:
    """Los nodos por debajo de su mínimo, del peor al mejor."""
    malos = []
    for pantalla, nodos in medicion.items():
        for n in nodos:
            tope = umbral if umbral is not None else n["min"]
            if n["r"] < tope:
                malos.append({**n, "pantalla": pantalla})
    return sorted(malos, key=lambda n: n["r"])
