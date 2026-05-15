#!/usr/bin/env python3
"""
build.py — Pipeline de compilación COBOL con inyección automática de conexión.

Para cada *.cbl en ./src ejecuta 5 pasos:
  1. INJECT  — Inyecta variables PG-* y bloque CONNECT/DISCONNECT en el fuente
  2. OCESQL  — Precompila EXEC SQL → llamadas COBOL
  3. COBC    — Compila con GnuCOBOL y enlaza libpq / ocesql
  4. DEPS    — Empaqueta todas las dependencias .so para despliegue en LPAR
  5. RPATH   — Parchea RPATH con patchelf para portabilidad

Las variables PG-* se leen de las variables de entorno PGHOST, PGDATABASE,
PGUSER, PGPORT y PGPASSWORD en tiempo de ejecución, igual que el psql/libpq
estándar. El programador sólo escribe lógica SQL; la conexión es transparente.
"""

import os
import re
import sys
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import Optional

# ── Colores ANSI ─────────────────────────────────────────────────────────────

RESET   = "\033[0m"
BOLD    = "\033[1m"
CYAN    = "\033[36m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
RED     = "\033[31m"
GRAY    = "\033[90m"
MAGENTA = "\033[35m"
WHITE   = "\033[97m"


def _log(tag: str, color: str, msg: str) -> None:
    print(f"{BOLD}{color}[{tag:^6}]{RESET} {msg}", flush=True)

def info(msg: str)   -> None: _log("INFO",   CYAN,    msg)
def ok(msg: str)     -> None: _log(" OK ",   GREEN,   msg)
def step(msg: str)   -> None: _log("STEP",   MAGENTA, msg)
def warn(msg: str)   -> None: _log("WARN",   YELLOW,  msg)
def error(msg: str)  -> None: _log("ERROR",  RED,     msg)
def inject(msg: str) -> None: _log("INJECT", YELLOW,  msg)
def lib(msg: str)    -> None: _log("LIB",    GRAY,    msg)
def cmd(msg: str)    -> None: _log("CMD",    GRAY,    msg)


# ── Fragmentos de código a inyectar ──────────────────────────────────────────

# Variables para leer las env-vars PG* (van FUERA del DECLARE SECTION,
# en el área WORKING-STORAGE normal).
_WS_PG_VARS = """\
       01 PG-HOST       PIC X(64) VALUE SPACES.
       01 PG-DB         PIC X(64) VALUE SPACES.
       01 PG-USER       PIC X(64) VALUE SPACES.
       01 PG-PORT       PIC X(10) VALUE SPACES.
       01 PG-PASS       PIC X(64) VALUE SPACES.
"""

# Variables de conexión para OCESQL (van DENTRO del DECLARE SECTION,
# son host variables de un solo byte porque el CONNECT usa :W-USR/:W-PWD/:W-DB
# sin datos reales; la contraseña la resuelve libpq vía PGPASSWORD).
_DECLARE_CONN_VARS = """\
       01 W-DB          PIC X(1) VALUE SPACES.
       01 W-USR         PIC X(1) VALUE SPACES.
       01 W-PWD         PIC X(1) VALUE SPACES.
"""

# Bloque que se inyecta justo después del primer párrafo del PROCEDURE DIVISION.
# Lee las variables de entorno y llama a CONNECT.
_CONNECT_BLOCK_HEADER = """\
           ACCEPT PG-HOST FROM ENVIRONMENT "PGHOST"
           ACCEPT PG-DB   FROM ENVIRONMENT "PGDATABASE"
           ACCEPT PG-USER FROM ENVIRONMENT "PGUSER"
           ACCEPT PG-PORT FROM ENVIRONMENT "PGPORT"
           ACCEPT PG-PASS FROM ENVIRONMENT "PGPASSWORD"
"""

_CONNECT_BLOCK_DISPLAYS = """\
           DISPLAY " "
           DISPLAY "  +------------------------------------------+"
           DISPLAY "  |      PostgreSQL  Connection              |"
           DISPLAY "  +------------------------------------------+"
           DISPLAY "  |  Host     : " PG-HOST
           DISPLAY "  |  Database : " PG-DB
           DISPLAY "  |  User     : " PG-USER
           DISPLAY "  |  Port     : " PG-PORT
           IF PG-PASS = SPACES
               DISPLAY "  |  Password : <NO DEFINIDA>"
           ELSE
               DISPLAY "  |  Password : ********"
           END-IF
           DISPLAY "  +------------------------------------------+"
           DISPLAY " "
           DISPLAY "  >> Connecting..."
"""

_CONNECT_BLOCK_FOOTER = """\
           EXEC SQL
               CONNECT :W-USR IDENTIFIED BY :W-PWD USING :W-DB
           END-EXEC
           IF SQLCODE NOT = 0
               DISPLAY "  !! Connection error"
               DISPLAY "     SQLCODE  : " SQLCODE
               DISPLAY "     SQLSTATE : " SQLSTATE
               DISPLAY " "
               STOP RUN
           END-IF
"""


def make_connect_block(show_connection: bool) -> str:
    if show_connection:
        return _CONNECT_BLOCK_HEADER + _CONNECT_BLOCK_DISPLAYS + _CONNECT_BLOCK_FOOTER
    return _CONNECT_BLOCK_HEADER + _CONNECT_BLOCK_FOOTER

# Líneas que se inyectan ANTES de GOBACK (GOBACK se conserva para cerrar el párrafo con punto).
_DISCONNECT_LINE = '           EXEC SQL DISCONNECT ALL END-EXEC\n'

SHOW_CONNECTION_FLAG = "--show-connection"
HELP_FLAG = "--help"


# ── PASO 1: Inyección de código ───────────────────────────────────────────────

# Patrones de detección (compilados una vez)
_RE_WS      = re.compile(r'WORKING-STORAGE\s+SECTION',         re.IGNORECASE)
_RE_DECLARE     = re.compile(r'EXEC\s+SQL\s+BEGIN\s+DECLARE\s+SECTION', re.IGNORECASE)
_RE_DECLARE_END = re.compile(r'EXEC\s+SQL\s+END\s+DECLARE\s+SECTION',   re.IGNORECASE)
_RE_PROC    = re.compile(r'PROCEDURE\s+DIVISION',               re.IGNORECASE)
_RE_PARA    = re.compile(r'^\s+[A-Za-z][A-Za-z0-9-]*\.\s*$')   # párrafo COBOL
_RE_GOBACK  = re.compile(r'^\s+(GOBACK|STOP\s+RUN)[\s.]*$',     re.IGNORECASE)


def inject_connection(src: Path, dst: Path, show_connection: bool = False) -> list:
    """
    Lee `src`, inyecta el boilerplate de conexión y escribe el resultado en `dst`.

    Devuelve una lista de strings de auditoría que documentan exactamente
    qué líneas se añadieron y dónde.

    Transformaciones aplicadas:
      A) Antes de BEGIN DECLARE SECTION  → inserta variables PG-HOST/DB/USER/PORT/PASS
         (fuera del DECLARE, son vars COBOL normales para ACCEPT FROM ENVIRONMENT)
      B) Justo tras BEGIN DECLARE SECTION → inserta W-DB/W-USR/W-PWD
         (dentro del DECLARE, son host variables para el CONNECT de OCESQL)
      C) Tras el primer párrafo del PROCEDURE DIVISION → inserta bloque CONNECT
         (lee env-vars, conecta y valida; los `DISPLAY` son opcionales)
      D) GOBACK → reemplazado por EXEC SQL DISCONNECT ALL
         (desconexión limpia; el programa termina por caída al END PROGRAM)
    """
    lines = src.read_text(encoding="utf-8").splitlines(keepends=True)

    out: list = []
    audit: list = []

    in_ws           = False
    in_proc         = False
    first_para_done = False

    for lineno, line in enumerate(lines, 1):

        # ── A) Detectar WORKING-STORAGE SECTION ──────────────────────────────
        if _RE_WS.search(line):
            in_ws = True
            out.append(line)
            continue

        # ── A→B) Cuando estamos en WS y encontramos BEGIN DECLARE SECTION: ───
        #   - Insertar PG-* vars ANTES de la línea DECLARE (transformación A)
        #   - Insertar W-* vars ANTES de END DECLARE SECTION (transformación B)
        if in_ws and _RE_DECLARE.search(line):
            in_ws = False

            # A — variables fuera del DECLARE (WORKING-STORAGE normal)
            out.append(_WS_PG_VARS)
            out.append("\n")
            out.append("\n")
            audit.append(
                f"  L{lineno:04d} [A] PG-HOST/DB/USER/PORT/PASS  ← antes de BEGIN DECLARE SECTION"
            )

            # la propia línea DECLARE
            out.append(line)
            # W-* se inyectarán al detectar END DECLARE SECTION (transformación B)
            continue

        # ── B) Antes de END DECLARE SECTION → inyectar W-* vars al final ───────
        # Colocar W-DB/W-USR/W-PWD DESPUÉS de las vars del usuario evita el bug
        # de OCESQL 1.4.0 que omite SetResultParams para PIC X cuando hay
        # variables PIC X(1) antes de las vars del usuario en el DECLARE SECTION.
        if not in_ws and _RE_DECLARE_END.search(line):
            out.append(_DECLARE_CONN_VARS)
            out.append(line)
            audit.append(
                f"  L{lineno:04d} [B] W-DB/W-USR/W-PWD            ← antes de END DECLARE SECTION"
            )
            continue

        # ── C) Detectar PROCEDURE DIVISION ───────────────────────────────────
        if _RE_PROC.search(line):
            in_proc = True
            out.append(line)
            continue

        # ── C) Primer párrafo en PROCEDURE DIVISION → bloque CONNECT ─────────
        if in_proc and not first_para_done and _RE_PARA.match(line):
            out.append(line)
            out.append(make_connect_block(show_connection))
            audit.append(
                f"  L{lineno:04d} [C] Bloque CONNECT{'/DISPLAY' if show_connection else ''}       ← en párrafo '{line.strip()}'"
            )
            first_para_done = True
            continue

        # ── D) Antes de GOBACK → inyectar DISCONNECT, conservar GOBACK ───────
        # GOBACK debe conservarse: es el que tiene el punto terminador del párrafo.
        # Sin él, END PROGRAM llega con el párrafo sin cerrar → syntax error.
        if in_proc and _RE_GOBACK.match(line):
            out.append(_DISCONNECT_LINE)
            out.append("\n")
            out.append(line)   # ← GOBACK original con su punto
            audit.append(
                f"  L{lineno:04d} [D] EXEC SQL DISCONNECT ALL      ← antes de {line.strip()} (conservado)"
            )
            continue

        out.append(line)

    content = "".join(out)
    if not content.endswith("\n"):
        content += "\n"
    dst.write_text(content, encoding="utf-8")
    return audit


# ── PASO 2/3: OCESQL + GnuCOBOL ──────────────────────────────────────────────

def run_cmd(args: list, label: str) -> None:
    """Ejecuta un comando externo; aborta con diagnóstico si falla."""
    cmd(f"{label}: {' '.join(str(a) for a in args)}")
    result = subprocess.run(args, capture_output=True)

    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")

    # Mostrar stdout (info)
    if stdout.strip():
        for ln in stdout.strip().splitlines():
            info(f"  {ln}")

    # Mostrar stderr: errores si falló, advertencias si tuvo éxito
    if stderr.strip():
        fn = error if result.returncode != 0 else warn
        for ln in stderr.strip().splitlines():
            fn(f"  {ln}")

    if result.returncode != 0:
        error(f"{label} falló con código de salida {result.returncode}")
        sys.exit(result.returncode)


# ── PASO 4: Empaquetado de dependencias ──────────────────────────────────────

# Libs del sistema que siempre estarán en el LPAR; no hace falta empaquetar
_SKIP_LIBS = re.compile(
    r'libc\.so|libm\.so|libpthread\.so|libdl\.so|librt\.so|ld-linux'
)


def _ldd_so_paths(binary: Path) -> list:
    """Devuelve la lista de rutas .so resueltas por ldd para `binary`."""
    result = subprocess.run(["ldd", str(binary)], capture_output=True, text=True)
    paths = []
    for m in re.finditer(r'=> (/[^ ]+)', result.stdout):
        so = m.group(1)
        if not _SKIP_LIBS.search(so):
            paths.append(so)
    return paths


def copy_deps(binary: Path, lib_dir: Path, _seen: Optional[set] = None) -> int:
    """
    Copia recursivamente todas las dependencias .so de `binary` a `lib_dir`.
    Evita duplicados con el conjunto `_seen`.
    Devuelve el número de nuevas librerías copiadas.
    """
    if _seen is None:
        _seen = set()
    copied = 0
    for so_path in _ldd_so_paths(binary):
        name = os.path.basename(so_path)
        if name in _seen:
            continue
        _seen.add(name)
        dest = lib_dir / name
        if not dest.exists():
            shutil.copy2(so_path, dest)
            lib(f"  copiada: {name}")
            copied += 1
        # Recursivo: dependencias de la dependencia
        copied += copy_deps(Path(so_path), lib_dir, _seen)
    return copied


# ── PASO 5: RPATH ─────────────────────────────────────────────────────────────

def patch_rpath(binary: Path, lib_dir: Path) -> None:
    """
    Configura RPATH para que:
      - el binario busque sus .so en ./lib  ($ORIGIN/lib)
      - cada .so busque sus propias deps en su mismo directorio  ($ORIGIN)
    Esto hace el paquete dist/ completamente auto-contenido.
    """
    subprocess.run(
        ["patchelf", "--set-rpath", "$ORIGIN/lib", str(binary)],
        check=True, capture_output=True
    )
    for so in lib_dir.glob("*.so*"):
        if so.is_file():
            # Ignorar errores en libs del sistema que rechacen el patcheo
            subprocess.run(
                ["patchelf", "--set-rpath", "$ORIGIN", str(so)],
                capture_output=True
            )


# ── Configuración del pipeline ────────────────────────────────────────────────

SRC_DIR     = Path("./src")
OUT_DIR     = Path("./dist")
OCESQL_COPY = Path("/usr/local/share/open-cobol-esql/copy")

COBC_FLAGS  = ["-x", "-fstatic-call"]
LINK_LIBS   = ["-locesql", "-lpq", "-lssl", "-lcrypto", "-lz"]


# ── Pipeline por programa ─────────────────────────────────────────────────────

def build_program(cbl_file: Path, tmp_dir: Path, show_connection: bool) -> None:
    program  = cbl_file.stem
    injected = tmp_dir / f"{program}_injected.cbl"
    cob_file = OUT_DIR / f"{program}.cob"
    exe_file = OUT_DIR / program
    lib_dir  = OUT_DIR / "lib"

    separator = f"{BOLD}{CYAN}{'─' * 64}{RESET}"
    print(f"\n{separator}")
    step(f"Procesando: {BOLD}{WHITE}{program}{RESET}")
    print(f"{separator}")

    # ── 1 / 5  Inyección ─────────────────────────────────────────────────────
    step("1/5  Inyectando gestión de conexión")
    audit = inject_connection(cbl_file, injected, show_connection=show_connection)
    if audit:
        inject(f"Transformaciones aplicadas en {cbl_file.name}:")
        for entry in audit:
            inject(entry)
    else:
        warn("No se encontraron puntos de inyección — ¿el fuente tiene WORKING-STORAGE y PROCEDURE DIVISION?")
    ok(f"Inyectado → {injected.name}")

    # ── 2 / 5  OCESQL ────────────────────────────────────────────────────────
    step("2/5  Precompilando EXEC SQL con OCESQL")
    run_cmd(["ocesql", str(injected), str(cob_file)], "ocesql")
    ok(f"Precompilado → {cob_file.name}")

    # ── 3 / 5  GnuCOBOL ─────────────────────────────────────────────────────
    step("3/5  Compilando con GnuCOBOL (cobc)")
    cobc_cmd = (
        ["cobc"]
        + COBC_FLAGS
        + [str(cob_file), "-I", str(OCESQL_COPY)]
        + LINK_LIBS
        + ["-o", str(exe_file)]
    )
    run_cmd(cobc_cmd, "cobc")
    ok(f"Binario → {exe_file}")

    # ── 4 / 5  Dependencias .so ──────────────────────────────────────────────
    step("4/5  Empaquetando dependencias .so")
    lib_dir.mkdir(parents=True, exist_ok=True)
    n = copy_deps(exe_file, lib_dir)
    ok(f"{n} librerías nuevas copiadas en {lib_dir}")

    # ── 5 / 5  RPATH ─────────────────────────────────────────────────────────
    step("5/5  Parcheando RPATH con patchelf")
    patch_rpath(exe_file, lib_dir)
    ok("RPATH: $ORIGIN/lib (binario)  /  $ORIGIN (libs)")

    exe_file.chmod(exe_file.stat().st_mode | 0o755)


# ── Resumen final ─────────────────────────────────────────────────────────────

def print_summary() -> None:
    separator = f"{BOLD}{GREEN}{'═' * 64}{RESET}"
    print(f"\n{separator}")
    print(f"{BOLD}{GREEN}  Build completado exitosamente{RESET}")
    print(f"{separator}\n")

    info(f"Binarios en {OUT_DIR}:")
    for f in sorted(OUT_DIR.iterdir()):
        if f.is_file():
            size = f.stat().st_size
            info(f"  {f.name:<30} {size:>12,} bytes")

    lib_dir = OUT_DIR / "lib"
    if lib_dir.exists():
        libs = sorted(lib_dir.glob("*.so*"))
        info(f"\nLibrerías empaquetadas en {lib_dir}:  ({len(libs)} archivos)")
        for so in libs:
            if so.is_file():
                size = so.stat().st_size
                lib(f"  {so.name:<42} {size:>10,} bytes")


# ── Punto de entrada ──────────────────────────────────────────────────────────

def print_usage() -> None:
    print(
        "usage: build.py [--show-connection] [--help]\n"
        "\n"
        "Options:\n"
        "  --show-connection   Inyecta los DISPLAY de conexión en el bloque CONNECT.\n"
        "  --help              Muestra esta ayuda.\n"
    )


def main() -> None:
    show_connection = SHOW_CONNECTION_FLAG in sys.argv
    if HELP_FLAG in sys.argv:
        print_usage()
        return

    cbl_files = sorted(SRC_DIR.glob("*.cbl"))
    if not cbl_files:
        error(f"No se encontraron archivos .cbl en {SRC_DIR}/")
        sys.exit(1)

    separator = f"{BOLD}{CYAN}{'═' * 64}{RESET}"
    print(f"\n{separator}")
    print(f"{BOLD}{CYAN}  COBOL Build Pipeline  —  {len(cbl_files)} programa(s) encontrado(s){RESET}")
    print(f"{separator}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        for cbl_file in cbl_files:
            build_program(cbl_file, tmp_dir, show_connection)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print_summary()


if __name__ == "__main__":
    main()
