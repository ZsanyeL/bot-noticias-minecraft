# -*- coding: utf-8 -*-
"""
Bot de noticias de Minecraft para Discord (en español)
=====================================================

Qué hace, cada vez que se ejecuta:
  1. Lee las noticias oficiales de Minecraft.net (artículos, snapshots,
     changelogs de Java y Bedrock, eventos, Marketplace...).
  2. Revisa la lista oficial de versiones de Mojang para avisar en cuanto sale
     una versión o snapshot nueva de Minecraft Java en el launcher.
  3. Traduce al español lo que venga en inglés.
  4. Publica lo nuevo en tu canal de Discord a través de un webhook.
  5. Guarda en "estado.json" lo que ya publicó, para no repetirlo.

No necesita instalar nada extra: solo usa herramientas que ya trae Python.
La dirección del webhook se lee de la variable DISCORD_WEBHOOK_URL (en GitHub
se guarda como "secreto", así nadie más la ve).
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# ---------------------------------------------------------------------------
# CONFIGURACIÓN — puedes cambiar estos valores sin tocar el resto del código
# ---------------------------------------------------------------------------

# Nombre con el que aparecerá el bot en Discord.
NOMBRE_DEL_BOT = "Noticias de Minecraft"

# Pon False si NO quieres avisos de versiones/snapshots de Java desde el launcher.
AVISAR_VERSIONES_JAVA = True

# Pon False si prefieres las noticias tal cual vienen (en inglés).
TRADUCIR_AL_ESPANOL = True

# Idiomas de Minecraft.net que se intentan para el enlace "Leer más",
# en orden de preferencia. Si el artículo no existe en español, se usa el inglés.
IDIOMAS_DE_ENLACE = ["es-mx", "es-es"]

# Para mencionar un rol en cada noticia, pon aquí su ID (por ejemplo "123456789012345678").
# Déjalo vacío ("") para no mencionar a nadie.
ID_ROL_A_MENCIONAR = ""

# Máximo de mensajes que el bot manda en una sola revisión (evita llenar el canal).
MAXIMO_MENSAJES_POR_REVISION = 5

# ---------------------------------------------------------------------------
# Fuentes oficiales (no cambiar salvo que Mojang las mueva)
# ---------------------------------------------------------------------------

URL_NOTICIAS = "https://www.minecraft.net/en-us/feeds/community-content/rss"
URL_VERSIONES = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
URL_TRADUCTOR = "https://translate.googleapis.com/translate_a/single"
# Plan B: si Minecraft.net no responde, se usan las noticias del launcher oficial.
URLS_RESPALDO = [
    "https://launchercontent.mojang.com/v2/javaNews.json",
    "https://launchercontent.mojang.com/v2/bedrockNews.json",
    "https://launchercontent.mojang.com/v2/javaPatchNotes.json",
]

ARCHIVO_ESTADO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "estado.json")
NAVEGADOR = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

# Colores de las tarjetas en Discord (formato hexadecimal convertido a número).
COLOR_NOTICIA = 0x3BA55C      # verde
COLOR_SNAPSHOT = 0xF1C40F     # amarillo
COLOR_BEDROCK = 0x5865F2      # azul
COLOR_VERSION = 0xE67E22      # naranja
COLOR_BIENVENIDA = 0x2ECC71

ATOM = "{http://www.w3.org/2005/Atom}"


# ---------------------------------------------------------------------------
# Utilidades de red
# ---------------------------------------------------------------------------

def descargar(url, tiempo=20, metodo="GET"):
    """Descarga una dirección web y devuelve los bytes (o None si falla)."""
    peticion = urllib.request.Request(
        url,
        method=metodo,
        headers={"User-Agent": NAVEGADOR, "Accept": "*/*", "Accept-Language": "es,en;q=0.8"},
    )
    try:
        with urllib.request.urlopen(peticion, timeout=tiempo) as respuesta:
            return respuesta.read()
    except Exception as error:  # cualquier fallo de red no debe tumbar el bot
        print(f"  [aviso] No se pudo abrir {url}: {error}")
        return None


def existe_pagina(url):
    """True si la página responde bien (sirve para ver si hay versión en español)."""
    peticion = urllib.request.Request(url, headers={"User-Agent": NAVEGADOR})
    try:
        with urllib.request.urlopen(peticion, timeout=15) as respuesta:
            final = respuesta.geturl()
            return respuesta.status == 200 and "/article/" in final and "/en-us/" not in final
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Traducción
# ---------------------------------------------------------------------------

def traducir(texto):
    """Traduce de inglés a español con el traductor gratuito de Google.
    Si algo falla, devuelve el texto original para no perder la noticia."""
    if not TRADUCIR_AL_ESPANOL or not texto or not texto.strip():
        return texto
    parametros = urllib.parse.urlencode(
        {"client": "gtx", "sl": "en", "tl": "es", "dt": "t", "q": texto}
    )
    datos = descargar(f"{URL_TRADUCTOR}?{parametros}", tiempo=15)
    if not datos:
        return texto
    try:
        resultado = json.loads(datos.decode("utf-8"))
        traducido = "".join(parte[0] for parte in resultado[0] if parte and parte[0])
        return traducido.strip() or texto
    except Exception as error:
        print(f"  [aviso] No se pudo traducir: {error}")
        return texto


# ---------------------------------------------------------------------------
# Lectura de fuentes
# ---------------------------------------------------------------------------

def decodificar_xml(datos):
    """Minecraft.net manda su RSS en UTF-16; esto lo convierte a texto normal."""
    if datos.startswith(b"\xff\xfe") or datos.startswith(b"\xfe\xff"):
        texto = datos.decode("utf-16")
    elif len(datos) > 1 and datos[1:2] == b"\x00":
        texto = datos.decode("utf-16-le")
    elif datos[:1] == b"\x00":
        texto = datos.decode("utf-16-be")
    else:
        texto = datos.decode("utf-8", errors="replace")
    texto = texto.lstrip("﻿")
    # quitamos la declaración <?xml ... encoding="utf-16"?> porque ya está decodificado
    return re.sub(r"^\s*<\?xml[^>]*\?>", "", texto)


def limpiar_html(texto):
    texto = re.sub(r"<[^>]+>", " ", texto or "")
    texto = html.unescape(texto)
    return re.sub(r"\s+", " ", texto).strip()


def leer_noticias():
    """Devuelve la lista de artículos de Minecraft.net (del más nuevo al más viejo)."""
    datos = descargar(URL_NOTICIAS)
    if not datos:
        return None
    try:
        raiz = ET.fromstring(decodificar_xml(datos))
    except ET.ParseError as error:
        print(f"  [aviso] El RSS de Minecraft.net no se pudo leer: {error}")
        return None

    articulos = []
    for item in raiz.iter("item"):
        titulo = limpiar_html(item.findtext("title"))
        enlace = ""
        enlace_atom = item.find(f"{ATOM}link")
        if enlace_atom is not None:
            enlace = enlace_atom.get("href", "")
        enlace = enlace or (item.findtext("link") or "").strip() or (item.findtext("guid") or "").strip()
        if not titulo or not enlace:
            continue
        fecha = None
        try:
            fecha = parsedate_to_datetime((item.findtext("pubDate") or "").strip())
            if fecha and fecha.tzinfo is None:
                fecha = fecha.replace(tzinfo=timezone.utc)
        except Exception:
            pass
        articulos.append(
            {
                "id": enlace.rstrip("/").split("/")[-1],
                "titulo": titulo,
                "resumen": limpiar_html(item.findtext("description")),
                "enlace": enlace,
                "fecha": fecha,
            }
        )
    articulos.sort(key=lambda a: a["fecha"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return articulos


def slug_desde_titulo(titulo):
    """Convierte "Minecraft 26.4 Snapshot 7" en "minecraft-26-4-snapshot-7" (como Minecraft.net)."""
    slug = re.sub(r"[^a-z0-9]+", "-", titulo.lower().replace(":", "").replace("'", ""))
    return slug.strip("-")


def leer_noticias_respaldo():
    """Plan B: noticias del launcher oficial de Mojang (mismo formato de artículos)."""
    articulos, vistos = [], set()
    for url in URLS_RESPALDO:
        datos = descargar(url)
        if not datos:
            continue
        try:
            entradas = json.loads(datos.decode("utf-8")).get("entries", [])
        except Exception:
            continue
        for e in entradas:
            titulo = str(e.get("title") or "").strip()
            if not titulo:
                continue
            enlace = str(e.get("readMoreLink") or "").strip()
            if not enlace.startswith("http"):
                enlace = f"https://www.minecraft.net/en-us/article/{slug_desde_titulo(titulo)}"
            identificador = enlace.split("?")[0].rstrip("/").split("/")[-1]
            if identificador in vistos:
                continue
            vistos.add(identificador)
            fecha = None
            try:
                fecha = datetime.fromisoformat(str(e.get("date", "")).replace("Z", "+00:00"))
                if fecha.tzinfo is None:
                    fecha = fecha.replace(tzinfo=timezone.utc)
            except Exception:
                pass
            articulos.append({
                "id": identificador,
                "titulo": titulo,
                "resumen": limpiar_html(str(e.get("shortText") or e.get("text") or "")),
                "enlace": enlace,
                "fecha": fecha,
            })
    articulos.sort(key=lambda a: a["fecha"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return articulos[:40] or None


def leer_versiones():
    """Devuelve {'release': 'x', 'snapshot': 'y'} con lo último del launcher de Java."""
    datos = descargar(URL_VERSIONES)
    if not datos:
        return None
    try:
        return json.loads(datos.decode("utf-8")).get("latest")
    except Exception as error:
        print(f"  [aviso] No se pudo leer la lista de versiones: {error}")
        return None


def imagen_del_articulo(url):
    """Busca la imagen principal del artículo (la que sale al compartirlo)."""
    datos = descargar(url, tiempo=15)
    if not datos:
        return None
    pagina = datos.decode("utf-8", errors="replace")
    for patron in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
    ):
        encontrado = re.search(patron, pagina, flags=re.I)
        if encontrado:
            imagen = html.unescape(encontrado.group(1))
            if imagen.startswith("/"):
                imagen = "https://www.minecraft.net" + imagen
            return imagen if imagen.startswith("https://") else None
    return None


def enlace_en_espanol(url):
    """Si el artículo existe en español en Minecraft.net, devuelve ese enlace."""
    if "/en-us/" not in url:
        return url
    for idioma in IDIOMAS_DE_ENLACE:
        candidato = url.replace("/en-us/", f"/{idioma}/")
        if existe_pagina(candidato):
            return candidato
    return url


# ---------------------------------------------------------------------------
# Clasificación: qué tipo de noticia es
# ---------------------------------------------------------------------------

def clasificar(titulo):
    t = titulo.lower()
    if "snapshot" in t or "pre-release" in t or "release candidate" in t:
        return "🧪 Snapshot de Java", COLOR_SNAPSHOT
    if re.search(r"\b(preview|beta)\b", t):
        return "📱 Preview de Bedrock", COLOR_BEDROCK
    if "bedrock" in t and "changelog" in t:
        return "📱 Actualización de Bedrock", COLOR_BEDROCK
    if "changelog" in t or "java edition" in t:
        return "⬆️ Actualización", COLOR_VERSION
    return "📰 Noticia", COLOR_NOTICIA


# ---------------------------------------------------------------------------
# Envío a Discord
# ---------------------------------------------------------------------------

def enviar_a_discord(webhook, tarjeta, contenido=""):
    cuerpo = {
        "username": NOMBRE_DEL_BOT,
        "embeds": [tarjeta],
        "allowed_mentions": {"parse": [], "roles": [ID_ROL_A_MENCIONAR] if ID_ROL_A_MENCIONAR else []},
    }
    if contenido:
        cuerpo["content"] = contenido
    datos = json.dumps(cuerpo).encode("utf-8")
    for intento in range(3):
        peticion = urllib.request.Request(
            webhook,
            data=datos,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "MinecraftNoticiasBot/1.0"},
        )
        try:
            with urllib.request.urlopen(peticion, timeout=20):
                return True
        except urllib.error.HTTPError as error:
            if error.code == 429:  # Discord pide esperar un momento
                espera = 2.0
                try:
                    espera = float(json.loads(error.read().decode()).get("retry_after", 2))
                except Exception:
                    pass
                time.sleep(espera + 0.5)
                continue
            print(f"  [error] Discord respondió {error.code}: {error.read().decode(errors='replace')[:300]}")
            return False
        except Exception as error:
            print(f"  [error] No se pudo enviar a Discord: {error}")
            time.sleep(2)
    return False


def mencion():
    return f"<@&{ID_ROL_A_MENCIONAR}>" if ID_ROL_A_MENCIONAR else ""


def tarjeta_de_articulo(articulo):
    etiqueta, color = clasificar(articulo["titulo"])
    # Los títulos de versiones ("Minecraft 26.4 Snapshot 7") se dejan como los escribe Mojang;
    # el traductor automático a veces los deforma ("Instantánea 7").
    titulo = articulo["titulo"] if color != COLOR_NOTICIA else traducir(articulo["titulo"])
    resumen = traducir(articulo["resumen"])
    if len(resumen) > 350:
        resumen = resumen[:347].rsplit(" ", 1)[0] + "…"
    enlace = enlace_en_espanol(articulo["enlace"])
    tarjeta = {
        "author": {"name": f"{etiqueta} • Minecraft.net"},
        "title": titulo[:256],
        "url": enlace,
        "description": (resumen + "\n\n" if resumen else "") + f"[Leer el artículo completo]({enlace})",
        "color": color,
        "footer": {"text": "Fuente oficial: Minecraft.net"},
    }
    if articulo["fecha"]:
        tarjeta["timestamp"] = articulo["fecha"].astimezone(timezone.utc).isoformat()
    imagen = imagen_del_articulo(articulo["enlace"])
    if imagen:
        tarjeta["image"] = {"url": imagen}
    return tarjeta


def tarjeta_de_version(tipo, version):
    if tipo == "snapshot":
        titulo = f"🧪 Nueva snapshot de Java: {version}"
        texto = (
            "Ya puedes probarla desde el launcher de Minecraft: activa **Snapshots** "
            "en *Instalaciones* y elige la versión más reciente.\n\n"
            "⚠️ Las snapshots son versiones de prueba: haz una copia de tu mundo antes de abrirlo."
        )
    else:
        titulo = f"🎉 Nueva versión de Minecraft Java: {version}"
        texto = "¡Ya está disponible en el launcher de Minecraft! Ábrelo y se actualizará solo."
    return {
        "author": {"name": "Launcher de Minecraft Java"},
        "title": titulo,
        "description": texto,
        "color": COLOR_VERSION if tipo == "release" else COLOR_SNAPSHOT,
        "footer": {"text": "Fuente oficial: lista de versiones de Mojang"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Memoria del bot (estado.json)
# ---------------------------------------------------------------------------

def cargar_estado():
    try:
        with open(ARCHIVO_ESTADO, encoding="utf-8") as archivo:
            return json.load(archivo)
    except (OSError, json.JSONDecodeError):
        return {}


def guardar_estado(estado):
    estado["articulos_publicados"] = estado.get("articulos_publicados", [])[-300:]
    with open(ARCHIVO_ESTADO, "w", encoding="utf-8") as archivo:
        json.dump(estado, archivo, ensure_ascii=False, indent=2)
        archivo.write("\n")


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------

def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook.startswith("http"):
        print("❌ Falta el webhook. Guarda la dirección como secreto DISCORD_WEBHOOK_URL en GitHub.")
        return 1

    estado = cargar_estado()
    primera_vez = not estado
    publicados = estado.setdefault("articulos_publicados", [])
    enviados = 0

    # --- 1. Noticias de Minecraft.net ---
    print("Revisando noticias de Minecraft.net...")
    articulos = leer_noticias()
    if articulos is None:
        print("  Minecraft.net no respondió; probando con las noticias del launcher...")
        articulos = leer_noticias_respaldo()
    if articulos is None:
        print("  No se pudieron leer las noticias esta vez; se intentará en la próxima revisión.")
    elif primera_vez:
        # La primera vez no inundamos el canal: guardamos todo como "ya visto"
        # y solo mandamos un saludo + la noticia más reciente como muestra.
        bienvenida = {
            "title": "✅ ¡Bot de noticias de Minecraft conectado!",
            "description": (
                "A partir de ahora publicaré aquí, en español:\n"
                "• 📰 Noticias oficiales de Minecraft.net\n"
                "• 🧪 Snapshots y versiones nuevas de Java\n"
                "• 📱 Previews y actualizaciones de Bedrock\n\n"
                "Reviso cada 15 minutos aproximadamente. Aquí va la noticia más reciente como muestra 👇"
            ),
            "color": COLOR_BIENVENIDA,
        }
        enviar_a_discord(webhook, bienvenida)
        time.sleep(1.5)
        if articulos:
            enviar_a_discord(webhook, tarjeta_de_articulo(articulos[0]))
            enviados += 1
        publicados.extend(a["id"] for a in reversed(articulos))
        print(f"  Primera ejecución: {len(articulos)} artículos marcados como vistos.")
    else:
        nuevos = [a for a in articulos if a["id"] not in publicados]
        nuevos = list(reversed(nuevos))[-MAXIMO_MENSAJES_POR_REVISION:]  # del más viejo al más nuevo
        for articulo in nuevos:
            print(f"  Nuevo: {articulo['titulo']}")
            if enviar_a_discord(webhook, tarjeta_de_articulo(articulo), mencion()):
                enviados += 1
                time.sleep(1.5)
            else:
                break  # si Discord falla, lo reintentamos en la próxima revisión
            publicados.append(articulo["id"])
        # artículos viejos que se saltaron por el límite también se marcan como vistos
        for articulo in articulos:
            if articulo["id"] not in publicados:
                publicados.append(articulo["id"])
        if not nuevos:
            print("  Nada nuevo.")

    # --- 2. Versiones de Java en el launcher ---
    if AVISAR_VERSIONES_JAVA:
        print("Revisando versiones de Java...")
        ultimas = leer_versiones()
        if ultimas:
            vistas = estado.setdefault("versiones", {})
            for tipo in ("release", "snapshot"):
                version = ultimas.get(tipo)
                if not version or vistas.get(tipo) == version:
                    continue
                if tipo == "snapshot" and version == ultimas.get("release"):
                    vistas[tipo] = version  # es la misma versión estable; no avisar dos veces
                    continue
                if tipo in vistas and not primera_vez:
                    print(f"  Nueva {tipo}: {version}")
                    if enviar_a_discord(webhook, tarjeta_de_version(tipo, version), mencion()):
                        enviados += 1
                        time.sleep(1.5)
                    else:
                        continue
                vistas[tipo] = version

    guardar_estado(estado)
    print(f"Listo. Mensajes enviados: {enviados}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
