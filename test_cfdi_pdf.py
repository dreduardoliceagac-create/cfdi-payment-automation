"""Pruebas locales: PDFs sintéticos y portal simulado, sin acceso a ERP."""
import argparse
from pathlib import Path
import sqlite3
import tempfile
import unittest

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from openpyxl import load_workbook
from playwright.sync_api import sync_playwright

from cfdi_pdf_core import (Bitacora, RevisionNecesaria, analizar_pdf, analizar_texto,
                           decision, documento)
from generar_cfdi_por_pdf import P, RECIBOS, procesar, todas_las_filas, filtrar_fechas, verificar_fechas, buscar_cliente
from generar_cfdi_por_pdf import estable, EsperaPortalAgotada, progreso
from generar_cfdi_por_pdf import ClienteNoEncontrado, exigir_cliente_unico
from generar_cfdi_por_pdf import elegir_recibos


def texto_factura(metodo="Pago en una sola exhibición", folio="1001", cliente="CLIENTE DE PRUEBA UNO"):
    return (f"Emisor: ERP\nSerie: DEM\nFolio: {folio}\nForma de pago: Tarjeta de débito\n"
            f"Método de Pago: {metodo}\nCond. de pago: \nReceptor:\nRFC: XAXX010101000\n"
            f"Nombre: {cliente}\nDomicilio fiscal: 14100\nUso CFDI: Gastos\nDescripción: productos")


def crear_pdf(texto):
    import io
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica"),
                             NameObject("/Encoding"): NameObject("/WinAnsiEncoding")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject(
        {NameObject("/F1"): writer._add_object(font)})})
    content = ["BT /F1 10 Tf 40 750 Td 15 TL"]
    for linea in texto.splitlines():
        linea = linea.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content.append(f"({linea}) Tj T*")
    content.append("ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(content).encode("cp1252"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    salida = io.BytesIO()
    writer.write(salida)
    return salida.getvalue()


class ReglasTest(unittest.TestCase):
    def test_recibo_especifico_conserva_numero_exacto(self):
        recibos=[{'numero':'000123'}, {'numero':'0001234'}]
        self.assertEqual(elegir_recibos(recibos, '000123'), recibos[:1])
        self.assertEqual(elegir_recibos(recibos, None), recibos)
        with self.assertRaises(RevisionNecesaria):
            elegir_recibos(recibos, '123')
        with self.assertRaises(RevisionNecesaria):
            elegir_recibos([recibos[0],recibos[0]], '000123')
    def test_cliente_ausente_distinto_de_duplicado(self):
        with self.assertRaises(ClienteNoEncontrado):
            exigir_cliente_unico([], 'CLIENTE AUSENTE', [{'text':'Seleccionar cliente'}])
        with self.assertRaises(RevisionNecesaria) as error:
            exigir_cliente_unico([{'value':'1'},{'value':'2'}], 'CLIENTE DUPLICADO', [])
        self.assertNotIsInstance(error.exception, ClienteNoEncontrado)
        exigir_cliente_unico([{'value':'1'}], 'CLIENTE UNICO', [])
    def test_folio_relleno_y_serie(self):
        self.assertEqual(documento("000DEM001001"), documento("DEM001001"))
        self.assertNotEqual(documento("DEM001001"), documento("OTR001001"))
        self.assertNotEqual(documento("DEM001001"), documento("DEM0010010"))
        for invalido in ("1001", "DEM001001/otra", "XX DEM001001"):
            with self.assertRaises(RevisionNecesaria):
                documento(invalido)

    def test_metodos(self):
        for metodo, esperado in [("PUE", "PUE"), ("PPD", "PPD"),
                                 ("Pago en una sola exhibición", "PUE"),
                                 ("Pago en parcialidades o diferido", "PPD"),
                                 ("PPD - Pago en parcialidades o diferido", "PPD")]:
            self.assertEqual(analizar_texto(texto_factura(metodo), "CLIENTE DE PRUEBA UNO", "000DEM001001")["metodo"], esperado)

    def test_no_confundir_forma_pago_ni_descripcion(self):
        texto = texto_factura("NO IDENTIFICADO") + "\nDescripción: PPD"
        with self.assertRaises(RevisionNecesaria):
            analizar_texto(texto, "CLIENTE DE PRUEBA UNO", "DEM001001")

    def test_pdf_contradictorio(self):
        for texto in (texto_factura("PUE - Pago en parcialidades o diferido"),
                      texto_factura() + "\nMétodo de pago: PPD"):
            with self.assertRaises(RevisionNecesaria):
                analizar_texto(texto, "CLIENTE DE PRUEBA UNO", "DEM001001")

    def test_receptor_y_folio_distintos(self):
        for cliente, factura in [("OTRA PERSONA", "DEM001001"), ("CLIENTE DE PRUEBA UNO", "DEM001002")]:
            with self.assertRaises(RevisionNecesaria):
                analizar_texto(texto_factura(), cliente, factura)

    def test_pdf_real_y_archivo_no_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "factura.pdf"
            ruta.write_bytes(crear_pdf(texto_factura()))
            self.assertEqual(analizar_pdf(ruta, "CLIENTE DE PRUEBA UNO", "000DEM001001")["metodo"], "PUE")
            ruta.write_text("<html>Sesión vencida</html>")
            with self.assertRaises(RevisionNecesaria):
                analizar_pdf(ruta, "CLIENTE DE PRUEBA UNO", "DEM001001")

    def test_recibos_mixtos_y_metodo_desconocido(self):
        self.assertEqual(decision(["PPD", "PPD"]), "APTO_PPD")
        self.assertEqual(decision(["PUE"]), "OMITIDO_PUE")
        self.assertEqual(decision(["PUE", "PPD"]), "REVISION_RECIBO_MIXTO")
        self.assertEqual(decision(["PPD", "DESCONOCIDO"]), "REVISION_NECESARIA")
        self.assertEqual(decision([]), "REVISION_NECESARIA")

    def test_persistencia_y_excel(self):
        with tempfile.TemporaryDirectory() as tmp:
            carpeta = Path(tmp)
            b = Bitacora(carpeta, "SUCURSALDEMO")
            b.iniciar_envio("0000010001")
            b.registrar("=FORMULA()", "0000010001", "000DEM001001", "SIMULACION_PPD", metodo="PPD")
            b.exportar(carpeta / "reporte.xlsx")
            b.db.close()
            b = Bitacora(carpeta, "SUCURSALDEMO")
            self.assertEqual(b.envio("0000010001"), "ENVIO_INICIADO")
            with self.assertRaises(sqlite3.IntegrityError):
                b.iniciar_envio("0000010001")
            libro = load_workbook(carpeta / "reporte.xlsx")
            self.assertEqual(libro.active["A2"].data_type, "s")
            self.assertEqual(libro.active["B2"].value, "000DEM001001")
            self.assertEqual(libro.active["D2"].value, "0000010001")
            self.assertEqual(libro.active.freeze_panes, "A2")
            libro.close()
            b.db.close()


class PortalSimuladoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.carpeta = Path(self.tmp.name)
        self.bitacora = Bitacora(self.carpeta, "SUCURSALDEMO")
        self.ctx = self.browser.new_context(accept_downloads=True)
        self.pagos, self.facturas = self.ctx.new_page(), self.ctx.new_page()
        self.recibo = dict(cliente="CLIENTE DE PRUEBA UNO", numero="0000010001", ya_cfdi=False)
        self.args = argparse.Namespace(pagos_desde="01/05/2026", pagos_hasta="31/05/2026",
                                       facturas_desde="01/04/2026", facturas_hasta="10/09/2026",
                                       emitir=True, espera_timbrado=1, sucursal="SucursalDemo")

    def tearDown(self):
        self.ctx.close()
        self.bitacora.db.close()
        self.tmp.cleanup()

    def preparar(self, metodos, confirma=True):
        self.ctx.route("https://prueba.invalid/pdf/*", lambda route: route.fulfill(
            status=200, headers={"Content-Type": "application/pdf", "Content-Disposition": "attachment; filename=factura.pdf"},
            body=crear_pdf(texto_factura(metodos[int(route.request.url.rsplit('/', 1)[-1])],
                                       str(1001 + int(route.request.url.rsplit('/', 1)[-1]))))))
        idp = P[1:]
        def lupa(js="return false"):
            return f'<input type="image" src="BuscarGV.png" onclick="{js}">'
        aplicadas = "".join(f"<tr><td>{lupa()}</td><td>000DEM{1001+i:06d}</td></tr>" for i in range(len(metodos)))
        self.pagos.set_content(f"""
            <script>window.emisiones=0</script>
            <h2>ERP de demostración (SucursalDemo) (2026-Sep)</h2>
            <input id='{idp}RBTipoSel_0' type='radio' checked>
            <input class='igte_EditInContainer' value='01/05/2026'>
            <input class='igte_EditInContainer' value='31/05/2026'>
            <table id='{idp}gvMovimientos'><tr><td>{lupa()}</td><td>CLIENTE DE PRUEBA UNO</td>
            <td>0000010001</td><td>$100.00</td><td>$0.00</td><td id='enviado'></td></tr></table>
            <div>Recibo:0000010001</div><table id='{idp}gVFacturas'>{aplicadas}</table>
            <div id='ctl00_Mensaje'></div>
            <button id='{idp}BTBuscar'>Buscar</button>
            <button id='{idp}UCCfdiPago1_BTCfdi' onclick="window.emisiones++;
            document.getElementById('enviado').textContent={'&quot;CFDI&quot;' if confirma else '&quot;&quot;'};
            document.getElementById('ctl00_Mensaje').textContent='Error al timbrado';">Generar</button>
        """)
        facturas = "".join(f"<tr><td>{lupa(f'window.elegida={i}; return false')}</td>"
                           f"<td>DEM{1001+i:06d}</td><td>09/05/2026</td><td>CLIENTE DE PRUEBA UNO</td></tr>"
                           for i in range(len(metodos)))
        self.facturas.set_content(f"""
            <div class='preClientesBusca'><input id='{idp}UCClientes1_TBNombre' type='text'></div>
            <select id='{idp}UCClientes1_DDClientes'><option value='123'>CLIENTE DE PRUEBA UNO</option></select>
            <input class='igte_EditInContainer' value='01/04/2026'>
            <input class='igte_EditInContainer' value='10/09/2026'>
            <input type='button' value='Buscar'>
            <table id='{idp}gvFacturas'>{facturas}</table>
            <input type='button' id='{idp}BTImprimeFactura' value='Imprimir Factura'
             onclick="const a=document.createElement('a'); a.href='https://prueba.invalid/pdf/'+window.elegida;
             document.body.appendChild(a); a.click(); a.remove();">
        """)

    def ejecutar_caso(self):
        import json
        procesar(self.pagos, self.facturas, self.recibo, self.bitacora, self.args, self.carpeta)
        estados = [json.loads(r[0])["estado"] for r in self.bitacora.db.execute("SELECT datos FROM facturas")]
        return estados, self.pagos.evaluate("window.emisiones")

    def test_pue_nunca_emite(self):
        self.preparar(["Pago en una sola exhibición"])
        self.assertEqual(self.ejecutar_caso(), (["OMITIDO_PUE"], 0))

    def test_cliente_inexistente_se_omite_y_continua(self):
        from unittest.mock import patch
        self.preparar(['PUE'])
        with patch('generar_cfdi_por_pdf.buscar_cliente', side_effect=ClienteNoEncontrado('Sin coincidencias')):
            self.assertEqual(self.ejecutar_caso(), (['OMITIDO_CLIENTE_NO_ENCONTRADO'], 0))
        self.assertIsNone(self.bitacora.envio(self.recibo['numero']))
        self.recibo = dict(self.recibo, numero='0000010002')
        self.pagos.locator(P+'gvMovimientos tr td').nth(2).evaluate("e=>e.textContent='0000010002'")
        self.pagos.get_by_text('Recibo:0000010001', exact=True).evaluate("e=>e.textContent='Recibo:0000010002'")
        self.assertEqual(self.ejecutar_caso(), (['OMITIDO_CLIENTE_NO_ENCONTRADO', 'OMITIDO_PUE'], 0))

    def test_cliente_duplicado_sigue_en_revision(self):
        self.preparar(['PUE'])
        self.facturas.locator('select').evaluate("e=>e.add(new Option('CLIENTE DE PRUEBA UNO','456'))")
        self.assertEqual(self.ejecutar_caso(), (['REVISION_NECESARIA'], 0))

    def preparar_cuentas_duplicadas(self, documentos_por_cuenta, metodos=None):
        self.args.emitir = False
        self.preparar(metodos or ['PPD'])
        self.facturas.evaluate("""cuentas => {
          const lista = document.querySelector('select');
          lista.replaceChildren();
          cuentas.forEach((_,i)=>lista.add(new Option('CLIENTE DE PRUEBA UNO', String(i+1))));
          const tabla=document.querySelector('[id$=gvFacturas]');
          const base=tabla.querySelector('tr').cloneNode(true);
          document.querySelector('input[value="Buscar"]').onclick=()=>{
            tabla.replaceChildren();
            cuentas[Number(lista.value)-1].forEach(doc=>{
              const fila=base.cloneNode(true);
              fila.cells[1].textContent=doc;
              fila.querySelector('input').onclick=()=>{window.elegida=Number(doc.match(/\\d+$/)[0])-1001;return false;};
              tabla.appendChild(fila);
            });
          };
          window.toquesFechas=0;
          document.querySelectorAll('.igte_EditInContainer').forEach(e=>{
            ['focus','input','change'].forEach(t=>e.addEventListener(t,()=>window.toquesFechas++));
          });
        }""", documentos_por_cuenta)

    def test_factura_en_tercera_de_cuatro_cuentas(self):
        import json
        self.preparar_cuentas_duplicadas([[], ['DEM009999'], ['DEM001001'], []])
        self.assertEqual(self.ejecutar_caso(), (['SIMULACION_PPD'], 0))
        datos=json.loads(self.bitacora.db.execute('select datos from facturas').fetchone()[0])
        self.assertEqual(datos['cuenta_cliente'], '3')
        self.assertEqual(self.facturas.evaluate('window.toquesFechas'), 0)

    def test_factura_en_primera_de_dos_cuentas(self):
        self.preparar_cuentas_duplicadas([['DEM001001'], []])
        self.assertEqual(self.ejecutar_caso(), (['SIMULACION_PPD'], 0))
        self.assertEqual(self.facturas.locator('select').input_value(), '1')

    def test_factura_ausente_en_todas_las_cuentas(self):
        self.preparar_cuentas_duplicadas([[], [], [], []])
        self.assertEqual(self.ejecutar_caso(), (['REVISION_NECESARIA'], 0))
        self.assertEqual(len(list((self.carpeta/'pdf').glob('*.pdf'))), 0)

    def test_facturas_de_un_recibo_en_cuentas_distintas(self):
        import json
        self.preparar_cuentas_duplicadas([['DEM001002'], ['DEM001001']], ['PPD','PPD'])
        self.assertEqual(self.ejecutar_caso(), (['SIMULACION_PPD']*2, 0))
        datos=[json.loads(r[0]) for r in self.bitacora.db.execute('select datos from facturas')]
        self.assertEqual({d['factura']:d['cuenta_cliente'] for d in datos},
                         {'000DEM001001':'2', '000DEM001002':'1'})

    def test_dos_clientes_con_ajax_pendiente_tras_descarga(self):
        from unittest.mock import patch
        self.args.emitir = False
        self.preparar(['PUE'])
        self.facturas.evaluate("""() => {
          window.ocupado = false;
          window.toquesFechas = 0;
          window.Sys = {WebForms:{PageRequestManager:{getInstance:()=>({get_isInAsyncPostBack:()=>window.ocupado})}}};
          const imprimir = document.querySelector('[id$=BTImprimeFactura]');
          const original = imprimir.onclick;
          imprimir.onclick = function(e) {window.ocupado=true; return original.call(this,e);};
          document.querySelector('[id$=UCClientes1_TBNombre]').onchange = function() {
            window.ocupado=false;
            document.querySelector('select').options[0].textContent=this.value;
            document.querySelector('[id$=gvFacturas] tr').cells[3].textContent=this.value;
          };
          document.querySelectorAll('.igte_EditInContainer').forEach(e => {
            ['focus','input','change'].forEach(tipo => e.addEventListener(tipo,()=>window.toquesFechas++));
          });
        }""")
        # Límite corto: la versión anterior falla aquí antes de escribir el segundo cliente de prueba.
        with patch('generar_cfdi_por_pdf.estable', side_effect=lambda page: estable(page, timeout_ms=2000, aviso_ms=500)):
            self.assertEqual(self.ejecutar_caso(), (['OMITIDO_PUE'], 0))
            self.assertTrue(self.facturas.evaluate('window.ocupado'))
            self.recibo = dict(cliente='CLIENTE DE PRUEBA DOS', numero='0000010002', ya_cfdi=False)
            self.pagos.locator(P + 'gvMovimientos tr td').nth(1).evaluate("e=>e.textContent='CLIENTE DE PRUEBA DOS'")
            self.pagos.locator(P + 'gvMovimientos tr td').nth(2).evaluate("e=>e.textContent='0000010002'")
            self.pagos.get_by_text('Recibo:0000010001', exact=True).evaluate("e=>e.textContent='Recibo:0000010002'")
            self.ctx.unroute('https://prueba.invalid/pdf/*')
            self.ctx.route('https://prueba.invalid/pdf/*', lambda route: route.fulfill(
                headers={'Content-Type':'application/pdf','Content-Disposition':'attachment; filename=fermin.pdf'},
                body=crear_pdf(texto_factura('PUE', cliente='CLIENTE DE PRUEBA DOS'))))
            self.assertEqual(self.ejecutar_caso(), (['OMITIDO_PUE', 'OMITIDO_PUE'], 0))
        self.assertEqual(len(list((self.carpeta / 'pdf').glob('*.pdf'))), 2)
        self.assertEqual(self.facturas.evaluate('window.toquesFechas'), 0)

    def test_ppd_emite_y_reinicio_no_duplica(self):
        self.preparar(["Pago en parcialidades o diferido"])
        self.assertEqual(self.ejecutar_caso(), (["TIMBRADO_CONFIRMADO"], 1))
        self.assertEqual(self.ejecutar_caso(), (["YA_TIMBRADO"], 1))

    def test_dos_recibos_mismo_cliente_tras_pdf(self):
        from unittest.mock import patch
        self.args.emitir = False
        self.preparar(['PPD'])
        self.facturas.evaluate("""() => {
          window.ocupado=false; window.ediciones=0; window.consultas=0;
          window.Sys={WebForms:{PageRequestManager:{getInstance:()=>({get_isInAsyncPostBack:()=>window.ocupado})}}};
          const campo=document.querySelector('[id$=UCClientes1_TBNombre]');
          campo.oninput=()=>window.ediciones++;
          campo.onchange=()=>window.ocupado=false;
          document.querySelector('input[value="Buscar"]').onclick=()=>{window.consultas++;window.ocupado=false;};
          const imprimir=document.querySelector('[id$=BTImprimeFactura]');
          const original=imprimir.onclick;
          imprimir.onclick=function(e){window.ocupado=true;return original.call(this,e);};
        }""")
        with patch('generar_cfdi_por_pdf.estable', side_effect=lambda page: estable(page, timeout_ms=2000, aviso_ms=500)):
            self.assertEqual(self.ejecutar_caso(), (['SIMULACION_PPD'], 0))
            ediciones = self.facturas.evaluate('window.ediciones')
            self.assertTrue(self.facturas.evaluate('window.ocupado'))
            self.recibo = dict(self.recibo, numero='0000010002')
            self.pagos.locator(P + 'gvMovimientos tr td').nth(2).evaluate("e=>e.textContent='0000010002'")
            self.pagos.get_by_text('Recibo:0000010001', exact=True).evaluate("e=>e.textContent='Recibo:0000010002'")
            self.assertEqual(self.ejecutar_caso(), (['SIMULACION_PPD', 'SIMULACION_PPD'], 0))
        self.assertEqual(self.facturas.evaluate('window.ediciones'), ediciones)
        self.assertEqual(self.facturas.evaluate('window.consultas'), 2)
        self.assertEqual(len(list((self.carpeta / 'pdf').glob('*.pdf'))), 2)
        verificar_fechas(self.facturas, '01/04/2026', '10/09/2026')

    def test_simulacion_ppd(self):
        self.args.emitir = False
        self.preparar(["PPD"])
        self.assertEqual(self.ejecutar_caso(), (["SIMULACION_PPD"], 0))

    def test_mixto_no_emite(self):
        self.preparar(["PUE", "PPD"])
        estados, emisiones = self.ejecutar_caso()
        self.assertEqual(estados, ["REVISION_RECIBO_MIXTO"] * 2)
        self.assertEqual(emisiones, 0)

    def test_dos_ppd_solo_una_emision(self):
        self.preparar(["PPD", "PPD"])
        estados, emisiones = self.ejecutar_caso()
        self.assertEqual(estados, ["TIMBRADO_CONFIRMADO"] * 2)
        self.assertEqual(emisiones, 1)

    def test_error_timbrado_no_es_exito_y_bloquea_reintento(self):
        self.preparar(["PPD"], confirma=False)
        self.assertEqual(self.ejecutar_caso(), (["TIMBRADO_NO_CONFIRMADO"], 1))
        self.assertEqual(self.ejecutar_caso(), (["BLOQUEADO_ENVIO_PREVIO"], 1))

    def test_factura_ausente_no_emite(self):
        self.preparar(["PPD"])
        self.facturas.locator(P + "gvFacturas tr td").nth(1).evaluate("e => e.textContent='DEM999999'")
        self.assertEqual(self.ejecutar_caso(), (["REVISION_NECESARIA"], 0))

    def test_pdf_de_otra_factura_no_emite(self):
        self.preparar(["PPD"])
        self.ctx.unroute("https://prueba.invalid/pdf/*")
        self.ctx.route("https://prueba.invalid/pdf/*", lambda route: route.fulfill(
            headers={"Content-Type": "application/pdf", "Content-Disposition": "attachment; filename=otra.pdf"},
            body=crear_pdf(texto_factura("PPD", folio="99999"))))
        self.assertEqual(self.ejecutar_caso(), (["REVISION_NECESARIA"], 0))

    def test_sucursal_distinta_no_emite(self):
        self.preparar(["PPD"])
        self.pagos.locator("h2").evaluate("e => e.textContent='ERP de demostración (Otra)' ")
        self.assertEqual(self.ejecutar_caso(), (["REVISION_NECESARIA"], 0))

    def test_recorrido_paginado(self):
        self.pagos.set_content("""
          <table id='ctl00_ContentPlaceHolder1_gvMovimientos'></table>
          <script>
            function mostrar(n) {
              let t = document.getElementById('ctl00_ContentPlaceHolder1_gvMovimientos');
              t.innerHTML = '<tr><td><input src="BuscarGV.png"></td><td>Cliente '+n+'</td></tr>'+
               '<tr><td><span>'+n+'</span>'+
               [1,2,3].filter(x=>x!==n).map(x=>`<a href="javascript:mostrar(${x})/*Page$${x}*/">${x}</a>`).join('')+'</td></tr>';
            }
            mostrar(2);
          </script>
        """)
        self.assertEqual([r[1] for r in todas_las_filas(self.pagos, RECIBOS)],
                         ["Cliente 1", "Cliente 2", "Cliente 3"])

    def test_scroll_incluye_filas_fuera_de_pantalla(self):
        self.pagos.set_content("<div style='height:100px;overflow:auto'><table id='ctl00_ContentPlaceHolder1_gvMovimientos'>" +
            "".join(f"<tr><td><input src='BuscarGV.png'></td><td>Cliente {i}</td></tr>" for i in range(150)) + "</table></div>")
        self.assertEqual(len(todas_las_filas(self.pagos, RECIBOS)), 150)

    def test_fechas_correctas_no_se_reescriben(self):
        self.pagos.set_content("""<script>window.cambios=0</script>
          <input class='igte_EditInContainer' value='1/4/2026' oninput='window.cambios++'>
          <input class='igte_EditInContainer' value='10/09/2026' oninput='window.cambios++'>""")
        filtrar_fechas(self.pagos, "01/04/2026", "10/09/2026")
        self.assertEqual(self.pagos.evaluate("window.cambios"), 0)

    def test_espera_postback_lento_hasta_terminar(self):
        self.pagos.set_content("""<script>
          window.ocupado = true;
          window.Sys = {WebForms:{PageRequestManager:{getInstance:()=>({get_isInAsyncPostBack:()=>window.ocupado})}}};
          setTimeout(()=>window.ocupado=false, 1200);
        </script>""")
        with self.assertLogs('cfdi_pdf', level='WARNING') as registros:
            estable(self.pagos, timeout_ms=4000, aviso_ms=200)
        self.assertFalse(self.pagos.evaluate('window.ocupado'))
        self.assertTrue(any('Esperando a ERP' in r for r in registros.output))

    def test_espera_agotada_informa_etapa(self):
        self.pagos.set_content("""<script>
          window.Sys = {WebForms:{PageRequestManager:{getInstance:()=>({get_isInAsyncPostBack:()=>true})}}};
        </script>""")
        progreso(self.pagos, 'Confirmar cliente de prueba')
        with self.assertRaisesRegex(EsperaPortalAgotada, 'Confirmar cliente de prueba'):
            estable(self.pagos, timeout_ms=200, aviso_ms=100)

    def test_timeout_interrumpe_y_registra_sin_emitir(self):
        import json
        from unittest.mock import patch
        self.preparar(['PPD'])
        with patch('generar_cfdi_por_pdf.seleccionar_recibo', side_effect=EsperaPortalAgotada('Etapa: recibo')):
            with self.assertRaises(EsperaPortalAgotada):
                self.ejecutar_caso()
        self.assertIsNone(self.bitacora.envio(self.recibo['numero']))
        datos = json.loads(self.bitacora.db.execute('SELECT datos FROM facturas').fetchone()[0])
        self.assertEqual(datos['estado'], 'REVISION_NECESARIA')
        self.assertIn('Etapa: recibo', datos['mensaje'])

    def test_cambiar_clientes_no_toca_fechas(self):
        self.preparar(["PPD"])
        self.facturas.evaluate("""() => {
          window.toquesFechas = 0;
          document.querySelectorAll('.igte_EditInContainer').forEach(e => {
            ['focus','input','change'].forEach(tipo => e.addEventListener(tipo, () => window.toquesFechas++));
          });
          const lista = document.querySelector('select');
          lista.add(new Option('CLIENTE DOS', '456'));
        }""")
        buscar_cliente(self.facturas, "CLIENTE DE PRUEBA UNO", "01/04/2026", "10/09/2026")
        buscar_cliente(self.facturas, "CLIENTE DOS", "01/04/2026", "10/09/2026")
        self.assertEqual(self.facturas.evaluate("window.toquesFechas"), 0)
        verificar_fechas(self.facturas, "01/04/2026", "10/09/2026")

    def test_fecha_cambiada_por_portal_se_detecta_sin_reescribir(self):
        self.preparar(["PPD"])
        self.facturas.locator("input[value='Buscar']").evaluate("""e => e.onclick = () => {
          document.querySelector('.igte_EditInContainer').value = '01/09/2026';
        }""")
        with self.assertRaisesRegex(RevisionNecesaria, "ERP muestra '01/09/2026'"):
            buscar_cliente(self.facturas, "CLIENTE DE PRUEBA UNO", "01/04/2026", "10/09/2026")
        self.assertEqual(self.facturas.locator('.igte_EditInContainer').first.input_value(), '01/09/2026')

    def test_fechas_recupera_reversion_del_control(self):
        self.pagos.set_content("""<script>window.revertida=false</script>
          <input class='igte_EditInContainer' value='01/09/2026'
            onchange="if(!window.revertida){this.value='01/09/2026';window.revertida=true}">
          <input class='igte_EditInContainer' value='10/09/2026'>""")
        filtrar_fechas(self.pagos, "01/04/2026", "10/09/2026")
        verificar_fechas(self.pagos, "01/04/2026", "10/09/2026")
        self.assertTrue(self.pagos.evaluate("window.revertida"))

    def test_fechas_error_incluye_valor_real(self):
        self.pagos.set_content("""<input class='igte_EditInContainer' value='01/09/2026'>
          <input class='igte_EditInContainer' value='10/09/2026'>""")
        with self.assertRaisesRegex(RevisionNecesaria, "ERP muestra '01/09/2026'"):
            verificar_fechas(self.pagos, "01/04/2026", "10/09/2026")


if __name__ == "__main__":
    unittest.main(verbosity=2)
