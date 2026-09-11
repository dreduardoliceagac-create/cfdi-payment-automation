"""ERP: dos pestañas, clasificación por PDF y CFDI sólo para recibos PPD."""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import logging
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from cfdi_pdf_core import Bitacora, RevisionNecesaria, analizar_pdf, decision, documento, normalizar

BASE = Path(__file__).resolve().parent

def cargar_entorno():
    ruta = BASE / ".env"
    if ruta.exists():
        for linea in ruta.read_text(encoding="utf-8-sig").splitlines():
            if "=" in linea and not linea.lstrip().startswith("#"):
                clave, valor = linea.split("=", 1)
                os.environ.setdefault(clave.strip(), valor.strip().strip("\"'"))


cargar_entorno()
PORTAL = os.getenv("ERP_URL", "https://erp.example.invalid/").rstrip("/") + "/"

P = "#ctl00_ContentPlaceHolder1_"
RECIBOS = P + "gvMovimientos"
APLICADAS = P + "gVFacturas"
FACTURAS = P + "gvFacturas"
LUPA = "input[src*='BuscarGV.png']"
FECHAS = "input.igte_EditInContainer"
VERSION = "2026-09-11-cuentas-por-factura"
LOG = logging.getLogger("cfdi_pdf")


class EsperaPortalAgotada(RuntimeError):
    """La página sigue ocupada: no iniciar otra acción sobre ese estado."""


class ClienteNoEncontrado(RevisionNecesaria):
    """Consulta terminada sin coincidencias: omitir y seguir con otro recibo."""


def exigir_cliente_unico(candidatos, cliente, opciones):
    if not candidatos:
        raise ClienteNoEncontrado(
            f"Cliente {cliente!r} no encontrado en Facturación; se omite sin generar CFDI. "
            f"Opciones disponibles: {[o['text'] for o in opciones][:12]!r}.")
    if len(candidatos) != 1:
        raise RevisionNecesaria(
            f"Cliente {cliente!r}: {len(candidatos)} coincidencias exactas en el desplegable. "
            "Revisar los registros duplicados; no se elige por aproximación.")


def progreso(page, mensaje):
    page._cfdi_etapa = mensaje
    LOG.info(mensaje)


def estable(page, timeout_ms=90_000, aviso_ms=10_000):
    page.wait_for_timeout(400)
    condicion = """() => {
        const g = window.Sys?.WebForms?.PageRequestManager?.getInstance?.();
        return document.readyState !== 'loading' && (!g || !g.get_isInAsyncPostBack());
    }"""
    etapa = getattr(page, "_cfdi_etapa", "esperar respuesta de ERP")
    inicio = time.monotonic()
    limite = inicio + timeout_ms / 1000
    while True:
        restante = limite - time.monotonic()
        if restante <= 0:
            raise EsperaPortalAgotada(
                f"ERP sigue ocupado tras {timeout_ms / 1000:g}s. Etapa: {etapa}. "
                f"Pestaña: {page.url}. Se detiene la corrida sin pasar al siguiente cliente.")
        try:
            page.wait_for_function(condicion, timeout=max(1, min(aviso_ms, int(restante * 1000))))
            break
        except PlaywrightTimeoutError:
            LOG.warning("Esperando a ERP (%.0fs): %s", time.monotonic() - inicio, etapa)
    if "login" in page.url.lower():
        raise RuntimeError("La sesión terminó. Se detiene la ejecución.")


def clic(page, locator):
    locator.click(timeout=45_000)
    estable(page)


def filtrar_fechas(page, inicio, fin):
    progreso(page, f"Configurar fechas iniciales: {inicio} a {fin}")
    estable(page)
    campos = page.locator(FECHAS)
    if campos.count() != 2:
        raise RevisionNecesaria("No se encontraron exactamente dos campos de fecha.")
    for intento in range(3):
        for i, fecha in enumerate((inicio, fin)):
            # El editor Infragistics conserva estado entre clientes. No vaciar
            # fechas correctas ni escribir mientras sigue procesando el servidor.
            estable(page)
            campo = page.locator(FECHAS).nth(i)
            if misma_fecha(campo.input_value(), fecha):
                continue
            campo.click()
            campo.press("Control+A")
            if intento == 0:
                campo.fill(fecha)
            else:
                campo.press_sequentially(fecha, delay=80)
            campo.press("Tab")
            estable(page)
        try:
            verificar_fechas(page, inicio, fin)
            return
        except RevisionNecesaria:
            if intento == 2:
                raise


def misma_fecha(actual, esperada):
    try:
        return datetime.strptime(actual.strip(), "%d/%m/%Y") == datetime.strptime(esperada, "%d/%m/%Y")
    except ValueError:
        return False


def verificar_fechas(page, inicio, fin):
    for i, valor in enumerate((inicio, fin)):
        actual = page.locator(FECHAS).nth(i).input_value().strip()
        if not misma_fecha(actual, valor):
            raise RevisionNecesaria(
                f"Fecha {'inicial' if i == 0 else 'final'} incorrecta en {page.url}: "
                f"se solicitó {valor!r}, ERP muestra {actual!r}.")


def verificar_sucursal(page, sucursal):
    # El encabezado observado es 'ERP de demostración (SucursalDemo) (2026-Sep)'.
    texto = normalizar(page.locator("body").inner_text())
    if not re.search(r"\(\s*" + re.escape(normalizar(sucursal)) + r"\s*\)", texto):
        raise RevisionNecesaria("La sucursal esperada no aparece en el encabezado de ERP.")


def filas(page, grid):
    page.locator(grid).wait_for(state="attached", timeout=45_000)
    return page.locator(f"{grid} tr").filter(has=page.locator(LUPA))


def leer_filas(page, grid):
    return filas(page, grid).evaluate_all("rows => rows.map(r => Array.from(r.cells, c => c.innerText.trim()))")


def paginar(page, grid, primera=False):
    links = page.locator(f"{grid} a[href*='Page$']")
    opciones = []
    for i in range(links.count()):
        href = links.nth(i).get_attribute("href") or ""
        match = re.search(r"Page\$(\w+)", href)
        if match:
            opciones.append((match[1], i))
    if not opciones:
        return False  # Los grids de las capturas usan scroll, sin paginación.
    if primera:
        elegido = next((i for v, i in opciones if v in {"First", "1"}), None)
    else:
        elegido = next((i for v, i in opciones if v == "Next"), None)
        if elegido is None:
            # GridView estándar: el número actual es un span, los demás son links.
            spans = page.locator(f"{grid} tr").filter(has=page.locator("a[href*='Page$']")).locator("span")
            actuales = [int(t.strip()) for t in spans.all_text_contents() if t.strip().isdigit()]
            if len(actuales) != 1:
                raise RevisionNecesaria("Paginador no reconocido; no se puede asegurar un recorrido completo.")
            mayores = [(int(v), i) for v, i in opciones if v.isdigit() and int(v) > actuales[0]]
            elegido = min(mayores)[1] if mayores else None
    if elegido is None:
        return False
    clic(page, links.nth(elegido))
    return True


def todas_las_filas(page, grid):
    paginar(page, grid, primera=True)
    vistas, resultado = set(), []
    while True:
        datos = leer_filas(page, grid)
        firma = repr(datos)
        if firma in vistas:
            raise RevisionNecesaria("El paginador repitió una página; recorrido incompleto.")
        vistas.add(firma)
        resultado.extend(datos)
        if not paginar(page, grid):
            return resultado


def localizar(page, grid, predicado):
    # Evitar un postback de paginación si la fila ya está presente: un cambio
    # de página puede borrar la selección del recibo activo en ERP.
    datos = leer_filas(page, grid)
    indices = [i for i, celdas in enumerate(datos) if predicado(celdas)]
    if len(indices) > 1:
        raise RevisionNecesaria("Más de una fila coincide con el documento.")
    if indices:
        return filas(page, grid).nth(indices[0])
    paginar(page, grid, primera=True)
    vistas = set()
    while True:
        datos = leer_filas(page, grid)
        firma = repr(datos)
        if firma in vistas:
            raise RevisionNecesaria("El paginador repitió una página al buscar el documento.")
        vistas.add(firma)
        indices = [i for i, celdas in enumerate(datos) if predicado(celdas)]
        if len(indices) > 1:
            raise RevisionNecesaria("Más de una fila coincide con el documento.")
        if indices:
            return filas(page, grid).nth(indices[0])
        if not paginar(page, grid):
            raise RevisionNecesaria("El documento dejó de estar disponible en el grid.")


def fila_recibo(page, recibo):
    return localizar(page, RECIBOS, lambda c: len(c) >= 5 and c[2] == recibo["numero"]
                     and normalizar(c[1]) == normalizar(recibo["cliente"]))


def confirmar_recibo(page, numero):
    patron = re.compile(r"Recibo:\s*" + re.escape(numero) + r"(?!\d)", re.I)
    limite = time.monotonic() + 45
    while time.monotonic() < limite:
        if patron.search(page.locator("body").inner_text()):
            return
        page.wait_for_timeout(500)
    raise RevisionNecesaria(f"No se confirmó el recibo activo {numero}.")


def seleccionar_recibo(page, recibo):
    page.bring_to_front()
    progreso(page, f"Seleccionar recibo {recibo['numero']} | {recibo['cliente']}")
    estable(page)
    radio = page.locator(P + "RBTipoSel_0")
    if not radio.is_checked():
        clic(page, radio)
    clic(page, fila_recibo(page, recibo).locator(LUPA))
    confirmar_recibo(page, recibo["numero"])
    if not radio.is_checked():
        raise RevisionNecesaria("La opción Facturas pagadas no quedó seleccionada.")


def seleccionar_aplicada(page, numero):
    page.bring_to_front()
    progreso(page, f"Seleccionar documento aplicado {numero}")
    fila = localizar(page, APLICADAS, lambda c: len(c) > 1 and c[1] == numero)
    clic(page, fila.locator(LUPA))


def buscar_cliente(page, cliente, inicio, fin, documentos=None):
    # Las fechas se establecen al abrir Facturación, una vez por ejecución.
    # Aquí sólo se leen: no reescribirlas ni recargar la pestaña entre clientes.
    page.bring_to_front()
    progreso(page, f"Facturación: buscar cliente {cliente}")
    # Igual que en la primera versión: introducir el cliente ANTES de esperar
    # el postback. La descarga puede dejar el indicador AJAX anterior activo;
    # esperar aquí impide incluso iniciar la siguiente consulta. Se conserva
    # la espera estricta después de Tab y de cada acción posterior.
    page.wait_for_load_state("domcontentloaded", timeout=45_000)
    verificar_fechas(page, inicio, fin)
    # La clase es visible en el árbol DOM de la captura 170035.
    campo = page.locator(P + "UCClientes1_TBNombre")
    if campo.count() != 1:
        raise RevisionNecesaria("No se identificó inequívocamente el campo Cliente de Facturación.")
    lista = page.locator(P + "UCClientes1_DDClientes")
    seleccion = lista.locator("option:checked")
    opciones_actuales = lista.locator("option").evaluate_all("os => os.map(o => ({value:o.value,text:o.textContent}))")
    candidatos_actuales = [o for o in opciones_actuales if normalizar(o['text']) == normalizar(cliente)]
    mismo_cliente = (
        normalizar(campo.input_value()) == normalizar(cliente)
        and seleccion.count() == 1
        and normalizar(seleccion.inner_text()) == normalizar(cliente)
    )
    if mismo_cliente:
        if len(candidatos_actuales) > 1:
            if not documentos:
                exigir_cliente_unico(candidatos_actuales, cliente, opciones_actuales)
            return resolver_cuentas(page, cliente, inicio, fin, documentos, candidatos_actuales)
        # fill(nombre_actual) + Tab no genera onchange. Tras una descarga,
        # esperar ese supuesto postback bloqueaba el siguiente recibo.
        progreso(page, f"Facturación: conservar cliente ya seleccionado {cliente}")
        consultar_facturas_cliente(page, cliente, inicio, fin)
        return {d: lista.input_value() for d in documentos or []}
    campo.fill(" ".join(cliente.split()))
    campo.press("Tab")
    progreso(page, f"Facturación: esperar lista de clientes para {cliente}")
    estable(page)
    lista = page.locator(P + "UCClientes1_DDClientes")
    limite = time.monotonic() + 30
    candidatos = []
    while time.monotonic() < limite:
        opciones = lista.locator("option").evaluate_all("os => os.map(o => ({value:o.value,text:o.textContent}))")
        candidatos = [o for o in opciones if normalizar(o["text"]) == normalizar(cliente)]
        if candidatos:
            break
        page.wait_for_timeout(300)
    if len(candidatos) > 1 and documentos:
        return resolver_cuentas(page, cliente, inicio, fin, documentos, candidatos)
    exigir_cliente_unico(candidatos, cliente, opciones)
    lista.select_option(value=candidatos[0]["value"])
    progreso(page, f"Facturación: confirmar cliente {cliente}")
    estable(page)
    verificar_fechas(page, inicio, fin)
    consultar_facturas_cliente(page, cliente, inicio, fin)
    if lista.input_value() != candidatos[0]['value']:
        raise RevisionNecesaria("ERP cambió la cuenta del cliente después de buscar.")
    return {d: candidatos[0]['value'] for d in documentos or []}


def activar_cuenta(page, cliente, cuenta, inicio, fin):
    page.bring_to_front()
    lista = page.locator(P + 'UCClientes1_DDClientes')
    verificar_fechas(page, inicio, fin)
    if lista.input_value() != cuenta:
        progreso(page, f"Facturación: seleccionar cuenta {cuenta} de {cliente}")
        lista.select_option(value=cuenta)
        estable(page)
    # Si era la cuenta activa, Buscar inicia la consulta sin esperar primero
    # un cambio de selección que no se produjo.
    consultar_facturas_cliente(page, cliente, inicio, fin)
    if lista.input_value() != cuenta:
        raise RevisionNecesaria("ERP cambió la cuenta consultada.")


def resolver_cuentas(page, cliente, inicio, fin, documentos, candidatos):
    cuentas = [o['value'] for o in candidatos]
    if len(set(cuentas)) != len(cuentas) or any(not c for c in cuentas):
        raise RevisionNecesaria("Las cuentas duplicadas no tienen identificadores distinguibles.")
    objetivos = {d: documento(d) for d in documentos}
    coincidencias = {d: [] for d in documentos}
    for indice, cuenta in enumerate(cuentas, 1):
        progreso(page, f"Revisar cuenta {indice} de {len(cuentas)} para {cliente}")
        activar_cuenta(page, cliente, cuenta, inicio, fin)
        for fila in todas_las_filas(page, FACTURAS):
            if len(fila) < 4 or normalizar(fila[3]) != normalizar(cliente):
                continue
            try:
                clave = documento(fila[1])
            except RevisionNecesaria:
                continue
            for factura, objetivo in objetivos.items():
                if clave == objetivo:
                    coincidencias[factura].append(cuenta)
    for factura, hallazgos in coincidencias.items():
        if len(hallazgos) != 1:
            raise RevisionNecesaria(
                f"Factura {factura}: {len(hallazgos)} coincidencias entre {len(cuentas)} cuentas "
                f"de {cliente}. Cuentas coincidentes: {hallazgos}. No se elige por orden.")
    resultado = {d: valores[0] for d, valores in coincidencias.items()}
    for factura, cuenta in resultado.items():
        progreso(page, f"Factura {factura} identificada en cuenta {cuenta}")
    return resultado


def consultar_facturas_cliente(page, cliente, inicio, fin):
    # Buscar sí inicia una nueva consulta, incluso para el mismo cliente.
    verificar_fechas(page, inicio, fin)
    progreso(page, f"Facturación: consultar facturas de {cliente}")
    clic(page, page.locator("input[value='Buscar']"))
    verificar_fechas(page, inicio, fin)
    lista = page.locator(P + "UCClientes1_DDClientes")
    if normalizar(lista.locator("option:checked").inner_text()) != normalizar(cliente):
        raise RevisionNecesaria("ERP cambió el cliente seleccionado al buscar.")


def descargar_factura(page, cliente, factura, destino):
    page.bring_to_front()
    progreso(page, f"Facturación: localizar factura {factura} de {cliente}")
    objetivo = documento(factura)
    def coincide(c):
        if len(c) < 4 or normalizar(c[3]) != normalizar(cliente):
            return False
        try:
            return documento(c[1]) == objetivo
        except RevisionNecesaria:
            return False
    todas = todas_las_filas(page, FACTURAS)
    if sum(coincide(c) for c in todas) != 1:
        raise RevisionNecesaria("La factura no aparece una sola vez para ese cliente y rango.")
    seleccionada = next(c for c in todas if coincide(c))
    if re.search(r"\bCANCELAD[AO]\b|NOTA DE CREDITO", normalizar(" ".join(seleccionada))):
        raise RevisionNecesaria("La factura aparece cancelada o como nota de crédito.")
    clic(page, localizar(page, FACTURAS, coincide).locator(LUPA))
    destino.parent.mkdir(parents=True, exist_ok=True)
    # Capturar tanto descarga directa como descarga iniciada por ventana auxiliar.
    descargas, nuevas = [], []
    def on_download(d):
        descargas.append(d)
    def on_page(pagina):
        nuevas.append(pagina)
        pagina.on("download", on_download)
    page.on("download", on_download)
    page.context.on("page", on_page)
    try:
        progreso(page, f"Descargar PDF de factura {factura} (máximo 60s)")
        page.locator(P + "BTImprimeFactura").click(timeout=45_000)
        limite = time.monotonic() + 60
        while not descargas and time.monotonic() < limite:
            page.wait_for_timeout(250)
        if len(descargas) != 1:
            raise RevisionNecesaria("No se recibió una única descarga PDF de Imprimir Factura.")
        descargas[0].save_as(str(destino))
    finally:
        page.remove_listener("download", on_download)
        page.context.remove_listener("page", on_page)
        for pagina in nuevas:
            pagina.remove_listener("download", on_download)
            if not pagina.is_closed():
                pagina.close()
    progreso(page, f"Leer y verificar PDF de factura {factura}")
    return analizar_pdf(destino, cliente, factura)


def enviado(page, recibo):
    celdas = fila_recibo(page, recibo).locator("td").all_text_contents()
    return normalizar(celdas[-1]) == "CFDI"


def enviado_sin_postback(page, recibo):
    for celdas in leer_filas(page, RECIBOS):
        if (len(celdas) >= 5 and celdas[2] == recibo["numero"]
                and normalizar(celdas[1]) == normalizar(recibo["cliente"])):
            return normalizar(celdas[-1]) == "CFDI"
    return False


def timbrar(page, recibo, bitacora, args, documentos):
    seleccionar_recibo(page, recibo)
    verificar_sucursal(page, args.sucursal)
    verificar_fechas(page, args.pagos_desde, args.pagos_hasta)
    if enviado(page, recibo):
        return "YA_TIMBRADO", "ERP ya muestra CFDI para este recibo."
    if bitacora.envio(recibo["numero"]):
        return "BLOQUEADO_ENVIO_PREVIO", "Existe un envío previo: cotejar en ERP antes de cualquier reintento."
    celdas = fila_recibo(page, recibo).locator("td").all_text_contents()
    for key, index in (("saldo", 4), ("importe", 3)):
        if key in recibo and normalizar(celdas[index]) != normalizar(recibo[key]):
            raise RevisionNecesaria("El importe o saldo del recibo cambió durante la revisión.")
    actuales = [c[1] for c in todas_las_filas(page, APLICADAS) if len(c) > 1]
    if sorted(actuales) != sorted(documentos):
        raise RevisionNecesaria("Las facturas aplicadas cambiaron antes de generar.")
    seleccionar_aplicada(page, documentos[-1])
    confirmar_recibo(page, recibo["numero"])
    boton = page.locator(P + "UCCfdiPago1_BTCfdi")
    if not boton.is_visible() or not boton.is_enabled():
        raise RevisionNecesaria("Generar CFDI de pago no está disponible.")
    bitacora.iniciar_envio(recibo["numero"])
    # Desde aquí no se vuelve a pulsar el botón, incluso después de reiniciar.
    boton.click(timeout=45_000)
    limite = time.monotonic() + args.espera_timbrado
    mensaje = ""
    while time.monotonic() < limite:
        page.wait_for_timeout(1000)
        aviso = page.locator("#ctl00_Mensaje")
        if aviso.count() and aviso.is_visible():
            mensaje = aviso.inner_text().strip()
        # La prueba de éxito es la columna CFDI de la fila exacta, no la palabra
        # 'timbrado', que también aparece en mensajes de error.
        if enviado_sin_postback(page, recibo):
            bitacora.confirmar_envio(recibo["numero"], mensaje)
            return "TIMBRADO_CONFIRMADO", mensaje or "Columna Enviado = CFDI."
        if re.search(r"error|no (?:se pudo|fue|ha sido)|rechaz|fall", mensaje, re.I):
            break
    # Un solo refresco de consulta; nunca reenviar una solicitud incierta.
    clic(page, page.locator(P + "BTBuscar"))
    if enviado(page, recibo):
        bitacora.confirmar_envio(recibo["numero"], mensaje)
        return "TIMBRADO_CONFIRMADO", mensaje or "Columna Enviado = CFDI tras consultar."
    return "TIMBRADO_NO_CONFIRMADO", mensaje or "Sin confirmación; envío bloqueado para revisión manual."


def procesar(pagos, facturas, recibo, bitacora, args, carpeta):
    cliente, numero = recibo["cliente"], recibo["numero"]
    documentos, resultados = [], []
    try:
        seleccionar_recibo(pagos, recibo)
        documentos = [c[1] for c in todas_las_filas(pagos, APLICADAS) if len(c) > 1]
        if not documentos or len(set(documentos)) != len(documentos):
            raise RevisionNecesaria("Recibo sin documentos o con documentos repetidos.")
        cuentas = buscar_cliente(facturas, cliente, args.facturas_desde, args.facturas_hasta, documentos)
        for factura in documentos:
            try:
                seleccionar_aplicada(pagos, factura)
                confirmar_recibo(pagos, numero)
                cuenta = cuentas[factura]
                if facturas.locator(P + 'UCClientes1_DDClientes').input_value() != cuenta:
                    activar_cuenta(facturas, cliente, cuenta, args.facturas_desde, args.facturas_hasta)
                nombre = hashlib.sha256(f"{cliente}|{numero}|{factura}".encode()).hexdigest()[:16] + "_" + uuid4().hex
                info = descargar_factura(facturas, cliente, factura, carpeta / "pdf" / (nombre + ".pdf"))
                info['cuenta_cliente'] = cuenta
                resultados.append((factura, info))
                bitacora.registrar(cliente, numero, factura, "PDF_VERIFICADO", **info)
            except Exception as error:
                if isinstance(error, EsperaPortalAgotada):
                    raise
                resultados.append((factura, {"metodo": "DESCONOCIDO", "mensaje": str(error)}))
                bitacora.registrar(cliente, numero, factura, "REVISION_NECESARIA", mensaje=str(error))
        estado = decision([i["metodo"] for _, i in resultados])
        mensaje = ""
        if estado == "APTO_PPD":
            if recibo["ya_cfdi"] or enviado(pagos, recibo):
                estado, mensaje = "YA_TIMBRADO", "ERP ya muestra CFDI para este recibo."
            elif bitacora.envio(numero):
                estado, mensaje = "BLOQUEADO_ENVIO_PREVIO", "Revisar el envío previo en ERP."
            elif args.emitir:
                # El recibo puede incluir varias facturas: todas se validaron antes
                # de emitir UNA sola vez para el recibo completo.
                seleccionar_recibo(pagos, recibo)
                actuales = [c[1] for c in todas_las_filas(pagos, APLICADAS) if len(c) > 1]
                if sorted(actuales) != sorted(documentos):
                    raise RevisionNecesaria("Las facturas aplicadas cambiaron durante la revisión.")
                estado, mensaje = timbrar(pagos, recibo, bitacora, args, documentos)
            else:
                estado = "SIMULACION_PPD"
        if estado == "REVISION_RECIBO_MIXTO":
            mensaje = "El recibo incluye PUE y PPD; el botón genera por recibo, requiere revisión."
        for factura, info in resultados:
            detalle = dict(info)
            detalle["mensaje"] = detalle.get("mensaje") or mensaje
            bitacora.registrar(cliente, numero, factura, estado, **detalle)
        print(f"{numero} | {cliente} | {estado}")
    except Exception as error:
        estado = "TIMBRADO_NO_CONFIRMADO" if bitacora.envio(numero) else "REVISION_NECESARIA"
        if isinstance(error, ClienteNoEncontrado) and not bitacora.envio(numero):
            estado = "OMITIDO_CLIENTE_NO_ENCONTRADO"
        for factura in documentos or [""]:
            info = dict(next((i for f, i in resultados if f == factura), {}))
            info["mensaje"] = str(error)
            bitacora.registrar(cliente, numero, factura, estado, **info)
        print(f"{numero} | {estado}: {error}")
        if isinstance(error, EsperaPortalAgotada):
            raise


def argumentos():
    parser = argparse.ArgumentParser(description=__doc__)
    for opcion in ("pagos-desde", "pagos-hasta", "facturas-desde", "facturas-hasta"):
        parser.add_argument("--" + opcion, required=True, help="DD/MM/AAAA")
    parser.add_argument("--sucursal", required=True, help="Debe aparecer en el encabezado de ERP")
    parser.add_argument("--emitir", action="store_true", help="Genera CFDI reales. Sin esto sólo consulta y registra.")
    parser.add_argument("--limite", type=int, help="Máximo de recibos a revisar en esta ejecución")
    parser.add_argument("--recibo", help="Revisar únicamente este número exacto de recibo dentro del rango")
    parser.add_argument("--espera-timbrado", type=int, default=180)
    parser.add_argument("--salida", type=Path, default=BASE / "resultados_cfdi_pdf")
    parser.add_argument("--login-manual", action="store_true", help="Entrar y elegir sucursal manualmente")
    args = parser.parse_args()
    try:
        for prefijo in ("pagos", "facturas"):
            desde, hasta = [getattr(args, prefijo + "_" + x) for x in ("desde", "hasta")]
            d, h = [datetime.strptime(x, "%d/%m/%Y") for x in (desde, hasta)]
            if d > h or d.strftime("%d/%m/%Y") != desde or h.strftime("%d/%m/%Y") != hasta:
                raise ValueError("Rango inválido; utilizar DD/MM/AAAA y desde <= hasta.")
        if (args.limite is not None and args.limite < 1) or args.espera_timbrado < 1:
            raise ValueError("Límite y espera deben ser positivos.")
        if not args.sucursal.strip():
            raise ValueError("Sucursal vacía.")
    except ValueError as error:
        parser.error(str(error))
    return args


def elegir_recibos(recibos, numero):
    if numero is None:
        return recibos
    encontrados = [r for r in recibos if r['numero'] == numero]
    if len(encontrados) != 1:
        raise RevisionNecesaria(f"El recibo {numero!r} no aparece una sola vez en el rango de pagos.")
    return encontrados


def login(page, manual):
    page.goto(PORTAL, wait_until="domcontentloaded")
    if manual:
        input("Inicia sesión y selecciona la sucursal en el navegador. Después pulsa Enter aquí... ")
    else:
        ruta = BASE / ".env"
        if ruta.exists():
            for linea in ruta.read_text(encoding="utf-8-sig").splitlines():
                if "=" in linea and not linea.lstrip().startswith("#"):
                    k, v = linea.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
        usuario, clave = os.getenv("ERP_USUARIO"), os.getenv("ERP_CONTRASENA")
        if not usuario or not clave:
            raise RuntimeError("Configura ERP_USUARIO y ERP_CONTRASENA en .env o usa --login-manual.")
        page.locator("#UCLogin1_ClaveUsuario").fill(usuario)
        page.locator("#UCLogin1_ClaveAcceso").fill(clave)
        page.locator("input[value='Entrar al sistema']").click()
        page.wait_for_url(re.compile(r"/SisStart\.aspx", re.I), timeout=45_000)
    estable(page)


def ejecutar():
    args = argumentos()
    from urllib.parse import urlparse
    servidor = urlparse(PORTAL)
    if servidor.scheme not in {"http", "https"} or not servidor.hostname or servidor.hostname.endswith(".invalid"):
        raise RuntimeError("Configura ERP_URL en .env con el portal autorizado antes de ejecutar. Las pruebas no requieren conexión al ERP.")
    # Comprobar dependencias antes de iniciar sesión.
    import pypdf  # noqa: F401
    import openpyxl  # noqa: F401
    carpeta = args.salida.resolve()
    bitacora = Bitacora(carpeta, normalizar(args.sucursal))
    reporte = carpeta / ("Revision_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".xlsx")
    LOG.setLevel(logging.INFO)
    formato = logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S")
    consola = logging.StreamHandler()
    archivo_log = logging.FileHandler(reporte.with_suffix(".log"), encoding="utf-8")
    for handler in (consola, archivo_log):
        handler.setFormatter(formato)
        LOG.addHandler(handler)
    LOG.info("Versión %s | Script: %s", VERSION, Path(__file__).resolve())
    print("MODO: " + ("EMISION REAL" if args.emitir else "SIMULACION (sin timbrar)"))
    print(f"Excel de resultados: {reporte}")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context(accept_downloads=True)
            pagos = context.new_page()
            progreso(pagos, "Abrir sesión")
            login(pagos, args.login_manual)
            progreso(pagos, "Abrir Aplicar pagos a facturas")
            pagos.goto(PORTAL + "CobRecepcionPagos.aspx")
            estable(pagos)
            verificar_sucursal(pagos, args.sucursal)
            facturas = context.new_page()
            progreso(facturas, "Abrir Facturación")
            facturas.goto(PORTAL + "VtaFacturas.aspx")
            estable(facturas)
            verificar_sucursal(facturas, args.sucursal)
            filtrar_fechas(facturas, args.facturas_desde, args.facturas_hasta)
            radio = pagos.locator(P + "RBEstatus_1")
            if not radio.is_checked():
                clic(pagos, radio)
            filtrar_fechas(pagos, args.pagos_desde, args.pagos_hasta)
            clic(pagos, pagos.locator(P + "BTBuscar"))
            verificar_fechas(pagos, args.pagos_desde, args.pagos_hasta)
            if not radio.is_checked():
                raise RuntimeError("Pagos aplicados no quedó seleccionado.")
            registros = todas_las_filas(pagos, RECIBOS)
            recibos, claves = [], set()
            for c in registros:
                if len(c) < 5:
                    raise RuntimeError("Cambió la estructura del grid de recibos.")
                key = (normalizar(c[1]), c[2])
                if key in claves:
                    raise RuntimeError("Recibo duplicado en el grid; revisar antes de emitir.")
                claves.add(key)
                recibos.append(dict(cliente=c[1], numero=c[2], importe=c[3], saldo=c[4],
                                    ya_cfdi=normalizar(c[-1]) == "CFDI", texto=" ".join(c)))
            recibos = elegir_recibos(recibos, args.recibo)
            for indice, recibo in enumerate(recibos[:args.limite], 1):
                if pagos.is_closed() or facturas.is_closed():
                    raise EsperaPortalAgotada("Se cerró una pestaña del agente; corrida interrumpida.")
                LOG.info("Recibo %s de %s", indice, len(recibos[:args.limite]))
                if not radio.is_checked():
                    raise RuntimeError("ERP cambió el filtro Pagos aplicados.")
                # Se conserva la regla del agente anterior: saldo <= $1.00.
                try:
                    saldo = Decimal(recibo["saldo"].replace("$", "").replace(",", "").strip())
                    if not saldo.is_finite() or saldo < 0:
                        raise InvalidOperation
                except InvalidOperation:
                    saldo = None
                if re.search(r"NOTA DE CREDITO|\bNCR\b", normalizar(recibo["texto"])):
                    bitacora.registrar(recibo["cliente"], recibo["numero"], "", "OMITIDO_NOTA_CREDITO")
                elif saldo is None or saldo > Decimal("1.00"):
                    bitacora.registrar(recibo["cliente"], recibo["numero"], "", "OMITIDO_SALDO",
                                       mensaje="Saldo ilegible o superior a $1.00; regla del agente anterior.")
                else:
                    procesar(pagos, facturas, recibo, bitacora, args, carpeta)
                bitacora.exportar(reporte)
            print(f"Finalizó la revisión de {min(len(recibos), args.limite or len(recibos))} recibos.")
            browser.close()
    except EsperaPortalAgotada as error:
        LOG.error("CORRIDA INCOMPLETA: %s", error)
    finally:
        try:
            bitacora.exportar(reporte)
        finally:
            bitacora.db.close()
            for handler in (consola, archivo_log):
                LOG.removeHandler(handler)
                handler.close()


if __name__ == "__main__":
    ejecutar()
