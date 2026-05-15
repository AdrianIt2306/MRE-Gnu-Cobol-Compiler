# COBOL + PostgreSQL — Docker Build for LPAR / Build con Docker para LPAR

> 🇺🇸 [English](#english) · 🇪🇸 [Español](#español)

---

## English

COBOL compilation environment with **PostgreSQL** access using **GnuCOBOL + OCESQL**.

Programs are compiled inside a Docker container and produce **portable Linux binaries** that are copied and executed directly on an LPAR — without installing anything on the target.

### How it works

```
┌─────────────────────────────────────┐
│  Windows (development)              │
│                                     │
│  src/MyProgram.cbl  ──────────┐     │
│                               ▼     │
│  Docker (debian:bookworm-slim)      │
│  ├─ GnuCOBOL                        │
│  ├─ OCESQL (SQL precompiler)        │
│  └─ build.py  (5 steps)             │
│       1. INJECT  — injects connection
│       2. OCESQL  — EXEC SQL → CALL  │
│       3. COBC    — compiles         │
│       4. DEPS    — packages .so     │
│       5. RPATH   — patches paths    │
│                               ▼     │
│  dist/MyProgram + dist/lib/  ──────►│
└─────────────────────────────────────┘
               │
               ▼
  LPAR Linux — runs ./MyProgram
  (reads PG* env vars automatically)
```

**The developer only writes business SQL.** The database connection is transparent: `build.py` injects it into the binary during compilation.

### Requirements (Windows only)

- Docker Desktop
- VS Code (optional)

GnuCOBOL does NOT need to be installed locally.

### Project structure

```
src/          ← COBOL programs (.cbl) — business logic only
dist/         ← Generated Linux binaries (copy to LPAR)
  lib/        ← Packaged .so libraries (no install needed on LPAR)
Dockerfile    ← Image with GnuCOBOL + OCESQL + libpq + Python 3
build.py      ← Compilation pipeline (5 steps, Python)
build.sh      ← Legacy bash script (replaced by build.py)
docker-compose.yml
```

### Compilation pipeline (`build.py`)

`build.py` is the core of the process. For each `*.cbl` in `src/` it runs 5 steps:

| Step | Name | Description |
|------|------|-------------|
| 1 | **INJECT** | Injects `PG-*` variables and `CONNECT`/`DISCONNECT` block into the source |
| 2 | **OCESQL** | Precompiles `EXEC SQL` → COBOL calls (`ocesql`) |
| 3 | **COBC** | Compiles with GnuCOBOL and links `libpq` / `libocesql` |
| 4 | **DEPS** | Recursively copies all required `.so` files to `dist/lib/` |
| 5 | **RPATH** | Patches the binary with `patchelf` (`$ORIGIN/lib`) for portability |

#### Transformations applied by INJECT

The injector analyzes the COBOL source and applies four transformations:

- **[A]** Inserts `PG-HOST / PG-DB / PG-USER / PG-PORT / PG-PASS` before `BEGIN DECLARE SECTION` (regular COBOL variables for `ACCEPT FROM ENVIRONMENT`)
- **[B]** Inserts `W-DB / W-USR / W-PWD` at the end of `DECLARE SECTION` (1-byte host variables for the OCESQL `CONNECT`)
- **[C]** After the first paragraph of `PROCEDURE DIVISION`, injects the block that reads env-vars and executes `CONNECT` with `SQLCODE` validation
- **[D]** Before each `GOBACK`, injects `EXEC SQL DISCONNECT ALL` (the original `GOBACK` is preserved to close the paragraph with a period)

### Writing a COBOL program with SQL

The developer **does not manage connections**. They only declare the host variables they need and write the `EXEC SQL` statements:

```cobol
       identification division.
       program-id. customers.

       data division.
       working-storage section.

           exec sql include sqlca end-exec.

           EXEC SQL BEGIN DECLARE SECTION END-EXEC.
       01 hv-total    pic 9(9) value 0.
           EXEC SQL END DECLARE SECTION END-EXEC.

       procedure division.
       start-here.
           EXEC SQL
               SELECT COUNT(*) INTO :hv-total FROM customers
           END-EXEC

           if sqlcode not = 0
               display "Error: " sqlcode
           else
               display "Total customers: " hv-total
           end-if

           goback.

       end program customers.
```

`build.py` automatically injects:
- Connection variables `PG-HOST/DB/USER/PORT/PASS` (to read `PG*` from the environment)
- `W-DB / W-USR / W-PWD` (1-byte host variables for OCESQL)
- `CONNECT` at startup with error validation
- `DISCONNECT` before each `GOBACK`

### Compile

```bash
docker compose build   # only the first time, or when Dockerfile changes
docker compose run --rm cobol-build
```

#### `build.py` options

```
usage: build.py [--show-connection] [--help]

  --show-connection   Injects the connection diagnostic DISPLAYs
                      (host, database, user, port, password).
  --help              Shows this help.
```

To enable the connection DISPLAYs:

```bash
docker compose run --rm cobol-build --show-connection
```

Binaries end up in `dist/`.

### Running on the LPAR

Copy the entire `dist/` folder to the LPAR (binary + `lib/`):

```bash
scp -r dist/ user@lpar:/store/programs/MYAPP/
```

Then on the LPAR:

```bash
export PGHOST=my-server
export PGDATABASE=my-database
export PGUSER=my-user
export PGPASSWORD=my-password
export PGPORT=5432          # optional, default 5432

/store/programs/MYAPP/customers
```

Expected output (without `--show-connection`):
```
Total customers: 000000042
```

Output with `--show-connection`:
```
  +------------------------------------------+
  |      PostgreSQL  Connection              |
  +------------------------------------------+
  |  Host     : my-server
  |  Database : my-database
  |  User     : my-user
  |  Port     : 5432
  |  Password : ********
  +------------------------------------------+

  >> Connecting...
Total customers: 000000042
```

> Binaries include all required `.so` files in `dist/lib/` with a relative RPATH.
> **No need to install GnuCOBOL, libpq, or any other library on the LPAR.**

### Adding a new program

1. Create `src/NewProgram.cbl` with the structure above
2. Run `docker compose run --rm cobol-build`
3. Copy `dist/NewProgram` and `dist/lib/` to the LPAR

### Common error diagnostics

| Error | Cause | Solution |
|-------|-------|----------|
| `SQLSTATE=08001` | Cannot connect to the server | Check `PGHOST`, `PGPORT`, network/firewall |
| `SQLSTATE=08003` | `PGPASSWORD` not defined or empty | `export PGPASSWORD=your-password` |
| `SQLSTATE=28000` | Wrong username/password | Check `PGUSER` and `PGPASSWORD` |
| `SQLSTATE=3D000` | Database does not exist | Check `PGDATABASE` |
| `libcob.so.4: not found` | Missing `dist/lib/` on target | Copy `dist/lib/` next to the binary |
| `No .cbl files found` | `src/` empty or wrongly mounted | Check the volume in `docker-compose.yml` |

---

## Español

Entorno de compilación COBOL con acceso a **PostgreSQL** usando **GnuCOBOL + OCESQL**.

Los programas se compilan en un contenedor Docker y producen **binarios Linux portables** que se copian y ejecutan directamente en un LPAR — sin instalar nada en el destino.

### Cómo funciona

```
┌─────────────────────────────────────┐
│  Windows (desarrollo)               │
│                                     │
│  src/MiPrograma.cbl  ─────────┐     │
│                               ▼     │
│  Docker (debian:bookworm-slim)      │
│  ├─ GnuCOBOL                        │
│  ├─ OCESQL (precompilador SQL)      │
│  └─ build.py  (5 pasos)             │
│       1. INJECT  — inyecta conexión │
│       2. OCESQL  — EXEC SQL → CALL  │
│       3. COBC    — compila          │
│       4. DEPS    — empaqueta .so    │
│       5. RPATH   — parchea paths    │
│                               ▼     │
│  dist/MiPrograma + dist/lib/  ─────►│
└─────────────────────────────────────┘
               │
               ▼
  LPAR Linux — ejecuta ./MiPrograma
  (lee PG* env vars automáticamente)
```

**El programador solo escribe SQL de negocio.** La conexión a la base de datos es transparente: `build.py` la inyecta en el binario durante la compilación.

### Requisitos (solo en Windows)

- Docker Desktop
- VS Code (opcional)

No se necesita GnuCOBOL instalado localmente.

### Estructura del proyecto

```
src/          ← Programas COBOL (.cbl) — solo lógica de negocio
dist/         ← Binarios Linux generados (copiar al LPAR)
  lib/        ← Librerías .so empaquetadas (sin instalar en LPAR)
Dockerfile    ← Imagen con GnuCOBOL + OCESQL + libpq + Python 3
build.py      ← Pipeline de compilación (5 pasos, Python)
build.sh      ← Script bash legado (reemplazado por build.py)
docker-compose.yml
```

### Pipeline de compilación (`build.py`)

`build.py` es el núcleo del proceso. Para cada `*.cbl` en `src/` ejecuta 5 pasos:

| Paso | Nombre | Descripción |
|------|--------|-------------|
| 1 | **INJECT** | Inyecta variables `PG-*` y bloque `CONNECT`/`DISCONNECT` en el fuente |
| 2 | **OCESQL** | Precompila `EXEC SQL` → llamadas COBOL (`ocesql`) |
| 3 | **COBC** | Compila con GnuCOBOL y enlaza `libpq` / `libocesql` |
| 4 | **DEPS** | Copia recursivamente todas las `.so` necesarias a `dist/lib/` |
| 5 | **RPATH** | Parchea el binario con `patchelf` (`$ORIGIN/lib`) para portabilidad |

#### Transformaciones que aplica INJECT

El inyector analiza el fuente COBOL y aplica cuatro transformaciones:

- **[A]** Inserta `PG-HOST / PG-DB / PG-USER / PG-PORT / PG-PASS` antes de `BEGIN DECLARE SECTION` (son variables COBOL normales para `ACCEPT FROM ENVIRONMENT`)
- **[B]** Inserta `W-DB / W-USR / W-PWD` al final del `DECLARE SECTION` (host variables de 1 byte para el `CONNECT` de OCESQL)
- **[C]** Tras el primer párrafo del `PROCEDURE DIVISION`, inyecta el bloque que lee las env-vars y ejecuta `CONNECT` con validación de `SQLCODE`
- **[D]** Antes de cada `GOBACK` inyecta `EXEC SQL DISCONNECT ALL` (el `GOBACK` original se conserva para cerrar el párrafo con punto)

### Escribir un programa COBOL con SQL

El programador **no gestiona conexiones**. Solo declara las host variables que necesita y escribe los `EXEC SQL`:

```cobol
       identification division.
       program-id. clientes.

       data division.
       working-storage section.

           exec sql include sqlca end-exec.

           EXEC SQL BEGIN DECLARE SECTION END-EXEC.
       01 hv-total    pic 9(9) value 0.
           EXEC SQL END DECLARE SECTION END-EXEC.

       procedure division.
       inicio.
           EXEC SQL
               SELECT COUNT(*) INTO :hv-total FROM customers
           END-EXEC

           if sqlcode not = 0
               display "Error: " sqlcode
           else
               display "Total clientes: " hv-total
           end-if

           goback.

       end program clientes.
```

`build.py` inyecta automáticamente:
- Variables de conexión `PG-HOST/DB/USER/PORT/PASS` (para leer `PG*` del entorno)
- `W-DB / W-USR / W-PWD` (host variables de 1 byte para OCESQL)
- `CONNECT` al inicio con validación de error
- `DISCONNECT` antes de cada `GOBACK`

### Compilar

```bash
docker compose build   # solo la primera vez o si cambia Dockerfile
docker compose run --rm cobol-build
```

#### Opciones de `build.py`

```
usage: build.py [--show-connection] [--help]

  --show-connection   Inyecta los DISPLAY de diagnóstico de conexión
                      (host, base, usuario, puerto, contraseña).
  --help              Muestra esta ayuda.
```

Para activar los DISPLAY de conexión:

```bash
docker compose run --rm cobol-build --show-connection
```

Los binarios quedan en `dist/`.

### Ejecutar en el LPAR

Copia la carpeta `dist/` completa al LPAR (binario + `lib/`):

```bash
scp -r dist/ usuario@lpar:/store/programs/MIAPP/
```

Luego en el LPAR:

```bash
export PGHOST=mi-servidor
export PGDATABASE=mi-base
export PGUSER=mi-usuario
export PGPASSWORD=mi-clave
export PGPORT=5432          # opcional, default 5432

/store/programs/MIAPP/clientes
```

Salida esperada (sin `--show-connection`):
```
Total clientes: 000000042
```

Salida con `--show-connection`:
```
  +------------------------------------------+
  |      PostgreSQL  Connection              |
  +------------------------------------------+
  |  Host     : mi-servidor
  |  Database : mi-base
  |  User     : mi-usuario
  |  Port     : 5432
  |  Password : ********
  +------------------------------------------+

  >> Connecting...
Total clientes: 000000042
```

> Los binarios incluyen todas las `.so` necesarias en `dist/lib/` con RPATH relativo.
> **No se necesita instalar GnuCOBOL, libpq ni ninguna otra librería en el LPAR.**

### Agregar un nuevo programa

1. Crear `src/NuevoPrograma.cbl` con la estructura de arriba
2. Ejecutar `docker compose run --rm cobol-build`
3. Copiar `dist/NuevoPrograma` y `dist/lib/` al LPAR

### Diagnóstico de errores comunes

| Error | Causa | Solución |
|-------|-------|----------|
| `SQLSTATE=08001` | No puede conectar al servidor | Verificar `PGHOST`, `PGPORT`, red/firewall |
| `SQLSTATE=08003` | `PGPASSWORD` no definida o vacía | `export PGPASSWORD=tu-clave` |
| `SQLSTATE=28000` | Usuario/contraseña incorrectos | Verificar `PGUSER` y `PGPASSWORD` |
| `SQLSTATE=3D000` | Base de datos no existe | Verificar `PGDATABASE` |
| `libcob.so.4: not found` | Falta `dist/lib/` en el destino | Copiar `dist/lib/` junto al binario |
| `No se encontraron archivos .cbl` | `src/` vacío o mal montado | Verificar volumen en `docker-compose.yml` |
