"""
La medida de sincronía de «Combinar MKV», ejecutada con señales sintéticas.

`mux_sync` es casi entero puro: la envolvente, la correlación, el reescalado y
el ajuste de la línea de tiempo se prueban aquí sin ffmpeg ni ficheros. El
«contenido» de una película es una serie de sucesos aleatorios (golpes,
arranques de frase); cada MKV lo ve en SU línea de tiempo: t_otro = α·t + β.

Los casos que importan son los que el usuario no podría ver sin la medida:
el caso real (23,976 contra 24 con 5,5 ppm de más), un logo de estudio más
largo (retraso constante), un corte de escena entre dos puntos, y el eco que
hace ambiguo un pico.
"""
import array
import math
import random
import sys
import unittest
from fractions import Fraction
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from phases import mux_sync  # noqa: E402
from phases.mux_sync import (  # noqa: E402
    DERIVA, EN_SINCRONIA, MONTAJE_DISTINTO, NO_CONCLUYENTE, OTRO_RITMO,
    RETRASO, ajustar, cadencia_de, correlar, elegir_ritmo, envolvente,
    pareja_de_referencia, reescalar, residuos,
)

HZ = mux_sync.ENVOLVENTE_HZ


def contenido(semilla: int, duracion_s: float, densidad: float = 3.0):
    """Sucesos (instante, amplitud) repartidos al azar por la película."""
    rnd = random.Random(semilla)
    n = int(duracion_s * densidad)
    return sorted((rnd.uniform(0, duracion_s), rnd.uniform(0.3, 1.0)) for _ in range(n))


def envolvente_de(sucesos, desde: float, n: int, *, alfa=1.0, beta=0.0,
                  cortes=()):
    """La envolvente (100 Hz) que vería un MKV cuya línea de tiempo es
    t = α·τ + β a partir de `desde` (segundos de ESE MKV).

    `cortes`: `[(τ_corte, segundos)]` — a partir de τ_corte, el contenido de
    este MKV va `segundos` más tarde (una escena añadida)."""
    sigma = 0.012
    env = [0.0] * n
    for tau, amp in sucesos:
        extra = sum(seg for tc, seg in cortes if tau >= tc)
        t = alfa * tau + beta + extra
        k0 = (t - desde) * HZ
        if -5 < k0 < n + 5:
            for k in range(max(0, int(k0) - 3), min(n, int(k0) + 4)):
                env[k] += amp * math.exp(-(((k - k0) / HZ) / sigma) ** 2)
    m = sum(env) / n
    return [v - m for v in env]


class TestCadencia(unittest.TestCase):

    def test_las_duraciones_de_frame_reales_dan_cadencias_exactas(self):
        # Los valores del caso real: el remux y el WEB-DL de Toy Story 5.
        self.assertEqual(cadencia_de(41708333), Fraction(24000, 1001))
        self.assertEqual(cadencia_de(41666666), Fraction(24))
        self.assertEqual(cadencia_de(41666667), Fraction(24))
        self.assertEqual(cadencia_de(40000000), Fraction(25))
        self.assertEqual(cadencia_de(16683333), Fraction(60000, 1001))

    def test_sin_dato_no_hay_cadencia(self):
        for v in (None, 0, -1, "x"):
            self.assertIsNone(cadencia_de(v))

    def test_una_cadencia_rara_se_devuelve_tal_cual(self):
        c = cadencia_de(41666437)
        self.assertNotIn(c, mux_sync._CADENCIAS)
        self.assertAlmostEqual(float(c), 1e9 / 41666437, places=4)


class TestEnvolvente(unittest.TestCase):

    def test_un_arranque_de_sonido_marca_su_bloque(self):
        sr = mux_sync.SR_HZ
        muestras = array.array("h", [0] * (2 * sr))
        for i in range(sr, sr + 400):       # un pitido que arranca en t = 1 s
            muestras[i] = int(8000 * math.sin(2 * math.pi * 440 * i / sr))
        env = envolvente(muestras)
        self.assertEqual(len(env), 2 * HZ)
        self.assertEqual(max(range(len(env)), key=env.__getitem__), 100)

    def test_sin_muestras_no_hay_envolvente(self):
        self.assertEqual(envolvente([]), [])


class TestCorrelar(unittest.TestCase):

    def test_encuentra_el_desplazamiento_con_precision_de_milisegundos(self):
        suc = contenido(1, 200)
        ref = envolvente_de(suc, 50.0, 30 * HZ)
        # El otro empieza 12,345 s antes de la ventana de referencia.
        otro = envolvente_de(suc, 50.0 - 12.345, 60 * HZ)
        k, r, c = correlar(ref, otro)
        self.assertAlmostEqual(k / HZ, 12.345, delta=0.003)
        self.assertGreater(r, 0.9)
        self.assertGreater(c, mux_sync.CONTRASTE_MINIMO)

    def test_un_eco_cercano_hace_el_pico_ambiguo(self):
        # Dos copias del contenido a 300 ms: el segundo máximo está DENTRO del
        # medio segundo que antes se excluía, y es casi tan alto. El punto no
        # puede darse por bueno (revisión de Codex del plan).
        suc = contenido(2, 200)
        eco = [(t + 0.3, a * 0.95) for t, a in suc]
        ref = envolvente_de(suc, 50.0, 30 * HZ)
        otro = envolvente_de(suc + eco, 40.0, 50 * HZ)
        _, r, c = correlar(ref, otro)
        self.assertLess(c, mux_sync.CONTRASTE_MINIMO)
        self.assertFalse(mux_sync.punto_valido(r, c))

    def test_un_tramo_tranquilo_junto_a_uno_activo_no_pierde(self):
        # La coincidencia está en un tramo de poca actividad junto a otro muy
        # activo: con la media de la ventana entera, el tramo tranquilo salía
        # penalizado. Centrado en su media, gana.
        suc = contenido(3, 200, densidad=0.8)
        ruido = contenido(4, 200, densidad=12.0)
        ruido = [(t, a) for t, a in ruido if 100 < t < 125]
        ref = envolvente_de(suc, 60.0, 30 * HZ)
        otro = envolvente_de(suc + ruido, 45.0, 90 * HZ)
        k, r, c = correlar(ref, otro)
        self.assertAlmostEqual(k / HZ, 15.0, delta=0.02)
        self.assertTrue(mux_sync.punto_valido(r, c))

    def test_una_referencia_plana_no_da_nada(self):
        self.assertEqual(correlar([0.0] * 100, [1.0] * 300), (0.0, 0.0, 0.0))
        self.assertEqual(correlar([1.0] * 100, [1.0] * 50), (0.0, 0.0, 0.0))


class TestPuntoConOtroRitmo(unittest.TestCase):
    """El caso real: 23,976 contra 24. Sin reescalar, los extremos de una
    ventana de 30 s se separan 30 ms y la medida dependía de DÓNDE cayera el
    sonido dentro de ella (revisión de Codex del plan)."""

    ALFA = Fraction(1000, 1001)

    def _medir(self, sucesos_en, t_a, beta=0.0):
        suc = contenido(5, 6200)
        suc = [(t, a) for t, a in suc if sucesos_en(t)] or suc
        ea = envolvente_de(suc, t_a, 30 * HZ)
        centro = float(self.ALFA) * t_a + beta
        desde_b = centro - 6.0
        eb = envolvente_de(suc, desde_b, 42 * HZ, alfa=float(self.ALFA), beta=beta)
        return mux_sync._punto(t_a, desde_b, ea, eb, self.ALFA)

    def test_reescalado_el_desfase_no_depende_de_donde_suena(self):
        t_a = 3000.0
        esperado = (float(self.ALFA) * t_a - t_a) * 1000.0
        al_principio = self._medir(lambda t: t_a <= t < t_a + 5, t_a)
        al_final = self._medir(lambda t: t_a + 25 <= t < t_a + 30, t_a)
        for p in (al_principio, al_final):
            self.assertAlmostEqual(p["desfase_ms"], esperado, delta=3.0)

    def test_sin_reescalar_se_nota(self):
        # El contraste de la prueba anterior: con α = 1 en la ventana, el
        # sonido al final de la ventana sale ~30 ms más tarde que al principio.
        t_a = 3000.0
        suc = [(t, a) for t, a in contenido(5, 6200) if t_a + 25 <= t < t_a + 30]
        ea = envolvente_de(suc, t_a, 30 * HZ)
        desde_b = float(self.ALFA) * t_a - 6.0
        eb = envolvente_de(suc, desde_b, 42 * HZ, alfa=float(self.ALFA))
        sin = mux_sync._punto(t_a, desde_b, ea, eb, Fraction(1))
        con = mux_sync._punto(t_a, desde_b, ea, eb, self.ALFA)
        self.assertGreater(abs(sin["desfase_ms"] - con["desfase_ms"]), 20.0)


def puntos(alfa=1.0, beta_ms=0.0, ts=None, ruido_ms=3.0, salto=None, invalidos=()):
    """Puntos medidos: d(T) = (α − 1)·T + β (+ un salto desde un T)."""
    ts = ts or [290.6, 1085.2, 1940.9, 2796.6, 3652.3, 4508.0, 5363.7]
    rnd = random.Random(7)
    out = []
    for i, t in enumerate(ts):
        d = (alfa - 1.0) * t * 1000.0 + beta_ms + rnd.uniform(-ruido_ms, ruido_ms)
        if salto and t >= salto[0]:
            d += salto[1]
        out.append({"t_a": t, "desfase_ms": d, "valido": i not in invalidos})
    return out


class TestAjustar(unittest.TestCase):

    def test_mismo_ritmo_y_sin_desfase(self):
        r = ajustar(puntos(), alfa_declarada=Fraction(1))
        self.assertEqual(r["veredicto"], EN_SINCRONIA)
        self.assertEqual((r["alfa_num"], r["alfa_den"]), (1, 1))

    def test_un_logo_mas_largo_es_un_retraso_constante(self):
        r = ajustar(puntos(beta_ms=2350.0), alfa_declarada=Fraction(1))
        self.assertEqual(r["veredicto"], RETRASO)
        self.assertAlmostEqual(r["beta_ms"], 2350, delta=5)

    def test_el_umbral_del_retraso_tiene_dos_lados(self):
        self.assertEqual(ajustar(puntos(beta_ms=6.0, ruido_ms=0.5),
                                 alfa_declarada=1)["veredicto"], EN_SINCRONIA)
        self.assertEqual(ajustar(puntos(beta_ms=14.0, ruido_ms=0.5),
                                 alfa_declarada=1)["veredicto"], RETRASO)

    def test_el_caso_real_es_otro_ritmo_con_el_ritmo_medido(self):
        # Los residuos medidos sobre Toy Story 5 (2026-10-25) con el ritmo
        # 1000/1001: suben de 6,6 a 34,7 ms. Eso son 5,5 ppm que 1000/1001 no
        # explica, y el ajuste los recoge en vez de dejarlos como error.
        res = [6.6, 11.2, 13.8, 16.1, 22.4, 30.0, 34.7]
        ts = [290.6, 1085.2, 1940.9, 2796.6, 3652.3, 4508.0, 5363.7]
        pts = [{"t_a": t, "desfase_ms": (1000 / 1001 - 1) * t * 1000 + rr, "valido": True}
               for t, rr in zip(ts, res)]
        r = ajustar(pts, alfa_declarada=Fraction(1000, 1001))
        self.assertEqual(r["veredicto"], OTRO_RITMO)
        alfa = Fraction(r["alfa_num"], r["alfa_den"])
        self.assertNotEqual(alfa, Fraction(1000, 1001))
        self.assertAlmostEqual((float(alfa) - 1000 / 1001) * 1e6, 5.5, delta=1.0)
        self.assertLess(r["dispersion_ms"], 10.0)

    def test_cerca_de_la_declarada_se_usa_la_exacta(self):
        r = ajustar(puntos(alfa=1000 / 1001 + 0.5e-6, ruido_ms=1.0),
                    alfa_declarada=Fraction(1000, 1001))
        self.assertEqual(r["veredicto"], OTRO_RITMO)
        self.assertEqual(Fraction(r["alfa_num"], r["alfa_den"]), Fraction(1000, 1001))

    def test_un_ritmo_que_nadie_declara_es_una_deriva(self):
        r = ajustar(puntos(alfa=1.00004), alfa_declarada=Fraction(1))
        self.assertEqual(r["veredicto"], DERIVA)
        self.assertAlmostEqual(r["pendiente_ppm"], 40.0, delta=2.0)

    def test_un_corte_entre_dos_puntos_es_un_montaje_distinto(self):
        r = ajustar(puntos(salto=(3000.0, 8000.0)), alfa_declarada=Fraction(1))
        self.assertEqual(r["veredicto"], MONTAJE_DISTINTO)
        self.assertEqual(r["salto_entre"], [2797, 3652])

    def test_pocos_puntos_validos_no_concluyen(self):
        r = ajustar(puntos(invalidos=(0, 2, 4)), alfa_declarada=Fraction(1))
        self.assertEqual(r["veredicto"], NO_CONCLUYENTE)
        self.assertEqual(r["n_validos"], 4)

    def test_cinco_validos_bastan(self):
        r = ajustar(puntos(invalidos=(1, 5)), alfa_declarada=Fraction(1))
        self.assertEqual(r["veredicto"], EN_SINCRONIA)

    def test_el_retraso_es_el_que_deja_menor_el_peor_error(self):
        # Residuos alternando ±19 ms: el retraso es 0 (todos a 19 ms), no el
        # −19 de la mediana (uno a 38 ms).
        base = puntos(ruido_ms=0.0)
        alternos = [dict(p, desfase_ms=p["desfase_ms"] + (19 if i % 2 else -19))
                    for i, p in enumerate(base)]
        r = ajustar(alternos, alfa_declarada=1)
        self.assertEqual(r["veredicto"], EN_SINCRONIA)
        self.assertEqual(r["beta_ms"], 0)

    def test_la_dispersion_tiene_su_frontera(self):
        # 38 ms de dispersión cuadra; 45, no (y no hay recta que lo explique).
        base = puntos(ruido_ms=0.0)
        bien = [dict(p, desfase_ms=p["desfase_ms"] + (19 if i % 2 else -19))
                for i, p in enumerate(base)]
        mal = [dict(p, desfase_ms=p["desfase_ms"] + (23 if i % 2 else -23))
               for i, p in enumerate(base)]
        self.assertEqual(ajustar(bien, alfa_declarada=1)["veredicto"], EN_SINCRONIA)
        self.assertEqual(ajustar(mal, alfa_declarada=1)["veredicto"], MONTAJE_DISTINTO)

    def test_residuos_con_el_ritmo(self):
        pts = [{"t_a": 1000.0, "desfase_ms": -999.0}]
        self.assertAlmostEqual(residuos(pts, Fraction(1000, 1001))[0], 0.0, delta=0.1)


class TestElegirRitmo(unittest.TestCase):

    def _ancla(self, t, d_por_ritmo):
        return {"t_a": t, "por_ritmo": {Fraction(a): {"t_a": t, "desfase_ms": d, "valido": True}
                                        for a, d in d_por_ritmo.items()}}

    def test_dos_anclas_eligen_el_ritmo_declarado(self):
        a = Fraction(1000, 1001)
        anclas = [self._ancla(300, {1: -300.0, a: -300.0}),
                  self._ancla(2800, {1: -2797.0, a: -2797.0})]
        self.assertEqual(elegir_ritmo(anclas, a), a)

    def test_si_no_se_separan_es_el_mismo_ritmo(self):
        a = Fraction(1000, 1001)
        anclas = [self._ancla(300, {1: 120.0, a: 120.0}),
                  self._ancla(2800, {1: 125.0, a: 125.0})]
        self.assertEqual(elegir_ritmo(anclas, a), Fraction(1))

    def test_el_ruido_de_dos_anclas_no_inventa_una_deriva(self):
        anclas = [self._ancla(300, {1: 10.0}), self._ancla(2800, {1: -15.0})]
        self.assertEqual(elegir_ritmo(anclas, Fraction(1)), Fraction(1))

    def test_sin_anclas_validas_manda_la_declarada(self):
        a = Fraction(1000, 1001)
        self.assertEqual(elegir_ritmo([{"t_a": 1, "por_ritmo": {}}], a), a)


class TestPareja(unittest.TestCase):

    def test_prefiere_el_mismo_idioma_y_no_el_comentario(self):
        a = [{"id": 1, "idioma": "eng", "nombre": "TrueHD Atmos", "por_defecto": True, "canales": 8},
             {"id": 18, "idioma": "eng", "nombre": "BTM Commentary", "canales": 2},
             {"id": 3, "idioma": "spa", "nombre": "DD 5.1", "canales": 6}]
        b = [{"id": 1, "idioma": "spa", "nombre": "Castellano TrueHD DCP", "por_defecto": True, "canales": 8},
             {"id": 4, "idioma": "eng", "nombre": "Inglés DD+ 5.1", "canales": 6}]
        self.assertEqual(pareja_de_referencia(a, b), (1, 4))

    def test_sin_papeles_el_mismo_idioma_con_a_y_b_cambiados(self):
        """Con el WEB-DL como A, su pista por defecto es el doblaje: el orden
        de A elegía castellano contra castellano, dos grabaciones distintas,
        y la medida no concluía (prueba real del 2026-10-27). Con el inglés
        preferido sale la misma pareja en las dos direcciones."""
        remux = [{"id": 1, "idioma": "eng", "nombre": "TrueHD Atmos", "por_defecto": True, "canales": 8},
                 {"id": 3, "idioma": "spa", "nombre": "DD 5.1", "canales": 6}]
        webdl = [{"id": 1, "idioma": "spa", "nombre": "Castellano TrueHD DCP", "por_defecto": True, "canales": 8},
                 {"id": 4, "idioma": "eng", "nombre": "Inglés DD+ 5.1", "canales": 6}]
        self.assertEqual(pareja_de_referencia(remux, webdl, ("eng",)), (1, 4))
        self.assertEqual(pareja_de_referencia(webdl, remux, ("eng",)), (4, 1))
        # Sin preferencia, el orden de A: el doblaje.
        self.assertEqual(pareja_de_referencia(webdl, remux), (1, 3))

    def test_la_version_original_va_delante_del_ingles(self):
        a = [{"id": 1, "idioma": "eng", "por_defecto": True, "canales": 8},
             {"id": 2, "idioma": "fre", "canales": 6}]
        b = [{"id": 5, "idioma": "eng", "canales": 6}, {"id": 6, "idioma": "fre", "canales": 6}]
        self.assertEqual(pareja_de_referencia(a, b, ("fre", "fra", "eng")), (2, 6))
        # Un preferido que no comparten no cuenta.
        self.assertEqual(pareja_de_referencia(a, b, ("jpn", "eng")), (1, 5))

    def test_sin_idioma_comun_las_principales(self):
        a = [{"id": 1, "idioma": "eng", "por_defecto": True, "canales": 8}]
        b = [{"id": 2, "idioma": "spa", "canales": 6}, {"id": 5, "idioma": "cat", "por_defecto": True, "canales": 6}]
        self.assertEqual(pareja_de_referencia(a, b), (1, 5))

    def test_sin_audio_no_hay_pareja(self):
        self.assertIsNone(pareja_de_referencia([], [{"id": 1, "idioma": "spa"}]))


class TestReescalar(unittest.TestCase):

    def test_identidad_y_longitud(self):
        s = [float(i) for i in range(1000)]
        self.assertEqual(reescalar(s, 1), s)
        r = reescalar(s, 0.5)
        self.assertEqual(len(r), 1999)
        self.assertAlmostEqual(r[3], 1.5)


if __name__ == "__main__":
    unittest.main()
