"""Redirige los directorios que la app resolvió AL IMPORTARSE (no es un test).

`storage.CONFIG_DIR` y lo que se deriva de él —`CMV40_DIR`, el índice del
sidebar de CMv4.0, `MKV_AUDIT_DIR`— se calculan del entorno en el `import`.
Cuatro módulos de la suite fijaban `os.environ["CONFIG_DIR"]` antes de
importar `storage`, y eso solo funciona si NADIE lo ha importado antes: la
suite entera pasaba porque uno de ellos se importa de los primeros, y con un
subconjunto de tests `test_mkv_cache` acabó escribiendo en el `/config` real.
Parchear los atributos ya resueltos funciona en cualquier orden, que es lo que
hace `api_harness` con los suyos.
"""
import tempfile
from pathlib import Path


def directorios_aislados(prefijo: str = "aislado_"):
    """Apunta `storage` y `paths` a un `/config` temporal.

    Devuelve `(config_dir, deshacer)`; `deshacer()` deja los originales."""
    import paths
    import storage
    config = Path(tempfile.mkdtemp(prefix=prefijo)) / "config"
    config.mkdir(parents=True)
    cambios = [
        (storage, "CONFIG_DIR", config),
        (storage, "CMV40_DIR", config / "cmv40"),
        (storage, "_CMV40_INDICE", config / "cmv40_summary.idx"),
        (storage, "MKV_AUDIT_DIR", config / "mkv_audits"),
        (storage, "MUX_DIR", config / "mux"),
        (paths, "CONFIG_DIR", config),
    ]
    originales = [(m, a, getattr(m, a)) for m, a, _ in cambios]
    for m, a, v in cambios:
        setattr(m, a, v)

    def deshacer():
        for m, a, v in originales:
            setattr(m, a, v)
    return config, deshacer
