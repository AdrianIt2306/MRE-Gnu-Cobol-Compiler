#!/bin/bash
set -e

SRC_DIR="./src"
OUT_DIR="./dist"
OCESQL_COPY="/usr/local/share/open-cobol-esql/copy"
TMP_DIR=$(mktemp -d)

mkdir -p "$OUT_DIR"

echo "=== Compilando programas COBOL ==="

for cbl_file in "$SRC_DIR"/*.cbl; do
    program=$(basename "$cbl_file" .cbl)
    injected_file="$TMP_DIR/${program}_injected.cbl"
    cob_file="$OUT_DIR/${program}.cob"
    exe_file="$OUT_DIR/${program}"

    echo ">>> Procesando: $program"

    # Paso 1: inyectar gestión de conexión automáticamente.
    # El programador escribe sólo lógica SQL; el build agrega
    # variables de conexión (W-*) y los CONNECT/DISCONNECT.
    # IMPORTANTE: OCESQL trunca nombres a 8 chars; W-DB/W-USR/W-PWD los cumplen.
    awk '
    BEGIN { in_proc=0; first_para_done=0; in_ws=0 }

    # Detectar WORKING-STORAGE SECTION para inyectar vars de env vars PG*
    toupper($0) ~ /WORKING-STORAGE[[:space:]]+SECTION/ { in_ws=1; print; next }

    # Después de WORKING-STORAGE inyectar vars para leer env vars (fuera de DECLARE)
    in_ws && toupper($0) ~ /EXEC SQL BEGIN DECLARE SECTION END-EXEC/ {
        in_ws=0
        print "       01 PG-HOST       PIC X(64) VALUE SPACES."
        print "       01 PG-DB         PIC X(64) VALUE SPACES."
        print "       01 PG-USER       PIC X(64) VALUE SPACES."
        print "       01 PG-PORT       PIC X(10) VALUE SPACES."
        print "       01 PG-PASS       PIC X(64) VALUE SPACES."
        print
        print
        print $0
        print "       01 W-DB          PIC X(1) VALUE SPACES."
        print "       01 W-USR         PIC X(1) VALUE SPACES."
        print "       01 W-PWD         PIC X(1) VALUE SPACES."
        next
    }

    # Detectar PROCEDURE DIVISION
    toupper($0) ~ /PROCEDURE[[:space:]]+DIVISION/ { in_proc=1; print; next }

    # Primer párrafo en PROCEDURE DIVISION -> leer env vars, CONNECT + chequeo
    in_proc && !first_para_done && /^[[:space:]]*[A-Za-z][A-Za-z0-9-]*\./ {
        print
        print "           ACCEPT PG-HOST FROM ENVIRONMENT \"PGHOST\""
        print "           ACCEPT PG-DB   FROM ENVIRONMENT \"PGDATABASE\""
        print "           ACCEPT PG-USER FROM ENVIRONMENT \"PGUSER\""
        print "           ACCEPT PG-PORT FROM ENVIRONMENT \"PGPORT\""
        print "           ACCEPT PG-PASS FROM ENVIRONMENT \"PGPASSWORD\""
        print "           DISPLAY \"DB PGHOST=\" PG-HOST"
        print "           DISPLAY \"DB PGDATABASE=\" PG-DB"
        print "           DISPLAY \"DB PGUSER=\" PG-USER"
        print "           DISPLAY \"DB PGPORT=\" PG-PORT"
        print "           IF PG-PASS = SPACES"
        print "               DISPLAY \"DB PGPASSWORD=<NO DEFINIDA>\""
        print "           ELSE"
        print "               DISPLAY \"DB PGPASSWORD=<definida>\""
        print "           END-IF"
        print "           DISPLAY \"DB Conectando...\""
        print "           EXEC SQL"
        print "               CONNECT :W-USR IDENTIFIED BY :W-PWD USING :W-DB"
        print "           END-EXEC"
        print "           IF SQLCODE NOT = 0"
        print "               DISPLAY \"DB ERROR SQLCODE=\" SQLCODE"
        print "               DISPLAY \"DB SQLSTATE=\" SQLSTATE"
        print "               STOP RUN"
        print "           END-IF"
        print "           DISPLAY \"DB Conexion exitosa.\""
        first_para_done=1
        next
    }

    # Antes de cualquier GOBACK, inyectar DISCONNECT
    in_proc && toupper($0) ~ /^[[:space:]]*GOBACK[[:space:].]*$/ {
        print "           EXEC SQL DISCONNECT ALL END-EXEC"
        print
        next
    }

    { print }
    ' "$cbl_file" > "$injected_file"

    # Paso 2: precompilar con OCESQL (EXEC SQL -> CALL statements)
    ocesql "$injected_file" "$cob_file"

    # Paso 3: compilar con GnuCOBOL (dinámico; las .so se empaquetan después)
    # -fstatic-call: resuelve CALL 'OCESQLConnect' etc. via linker, no via libcob runtime
    cobc -x -fstatic-call "$cob_file" \
        -I "$OCESQL_COPY" \
        -locesql \
        -lpq \
        -lssl \
        -lcrypto \
        -lz \
        -o "$exe_file"

    # Paso 4: empaquetar las .so junto al binario para no necesitar nada en el LPAR.
    # Se copian dependencias de forma recursiva (directas e indirectas) y se fija
    # el RPATH a $ORIGIN/lib en el binario y a $ORIGIN en cada .so empaquetada.
    LIB_DIR="$OUT_DIR/lib"
    mkdir -p "$LIB_DIR"

    copy_deps() {
        local bin="$1"
        ldd "$bin" 2>/dev/null \
            | grep -oP '=> \K/[^ ]+' \
            | grep -v 'libc\.so\|libm\.so\|libpthread\.so\|libdl\.so\|librt\.so\|ld-linux' \
            | while read -r so; do
                dest="$LIB_DIR/$(basename "$so")"
                if [ ! -f "$dest" ]; then
                    cp "$so" "$dest"
                    echo "      lib: $(basename "$so")"
                    copy_deps "$so"   # recursivo para deps de deps
                fi
            done
    }
    copy_deps "$exe_file"

    # Parchear RPATH: binario busca en ./lib; cada .so busca en su mismo directorio
    patchelf --set-rpath '$ORIGIN/lib' "$exe_file"
    for so in "$LIB_DIR"/*.so*; do
        [ -f "$so" ] && patchelf --set-rpath '$ORIGIN' "$so" 2>/dev/null || true
    done

    chmod +x "$exe_file"
    echo "    OK: $exe_file"
done

rm -rf "$TMP_DIR"

echo ""
echo "=== Binarios generados en $OUT_DIR ==="
ls -lh "$OUT_DIR"
echo ""
echo "=== Librerías empaquetadas en $OUT_DIR/lib ==="
ls -lh "$OUT_DIR/lib" 2>/dev/null || true
