import io
import os
import threading
import unittest
import urllib.request
from io import BytesIO

os.environ["AJUSTE_GRAVAR_BASE"] = "0"

import openpyxl

from pathlib import Path

from ajuste_fiscal import ler_origem, processar
from ajuste_fiscal.laudo import itens_do_texto, ler_laudo
from ajuste_fiscal.normalizar import casa_por_embalagem, codigo_cclass, codigo_ncm, tipo_status
from app import criar_servidor

CABECALHO = (
    "Identificador|Descricao|Código de Barras|NCM|CEST|"
    "Classif. tributária NFe|Classif. tributária Regular|Classif. tributária NFCe"
)


def planilha(linhas) -> bytes:
    livro = openpyxl.Workbook()
    aba = livro.active
    aba.title = "Produtos auditados"
    aba.append(["Código", "Descrição", "NCM", "ORCCA | Status NCM", "NCM para cadastro", "cClassTrib"])
    for linha in linhas:
        aba.append(linha)
    buffer = BytesIO()
    livro.save(buffer)
    return buffer.getvalue()


def csv_sistema(linhas, quebra="\r\n") -> bytes:
    texto = quebra.join([CABECALHO, *linhas])
    texto += quebra
    return texto.encode("latin-1")


class NormalizacaoTeste(unittest.TestCase):
    def test_ncm_com_pontos_e_zero_a_esquerda(self):
        self.assertEqual(codigo_ncm("3402.90.90"), "34029090")
        self.assertEqual(codigo_ncm(5040013), "05040013")
        self.assertEqual(codigo_ncm("05040013"), "05040013")
        self.assertIsNone(codigo_ncm("123456789"))
        self.assertIsNone(codigo_ncm(""))
        self.assertIsNone(codigo_ncm("12A34"))

    def test_cclass_com_seis_posicoes(self):
        self.assertEqual(codigo_cclass(1), "000001")
        self.assertEqual(codigo_cclass("000001"), "000001")
        self.assertEqual(codigo_cclass("200003"), "200003")
        self.assertIsNone(codigo_cclass("1234567"))
        self.assertIsNone(codigo_cclass(None))

    def test_status_definitivo(self):
        self.assertEqual(tipo_status("Correto"), "definitivo")
        self.assertEqual(tipo_status("Incorreto"), "definitivo")
        self.assertEqual(tipo_status("Revisar (análise do cliente)"), "excluido")
        self.assertEqual(tipo_status("Não é mercadoria"), "excluido")

    def test_embalagem_unica(self):
        self.assertTrue(casa_por_embalagem("SUKITA PET 200ML", "SUKITA PET 200ML SH C 12"))
        self.assertFalse(casa_por_embalagem("SKOL LATA 350ML", "SKOL LATA 350ML SH C 18 NPAL"))
        self.assertFalse(casa_por_embalagem("MOSTARDA", "MOSTARDA"))


class CamposFiscaisTeste(unittest.TestCase):
    def test_planilha_le_natureza_cest_e_ncm_anterior(self):
        from ajuste_fiscal.contabilidade import ler_contabilidade

        livro = openpyxl.Workbook()
        aba = livro.active
        aba.title = "Produtos auditados"
        aba.append(
            [
                "Código",
                "Descrição",
                "NCM",
                "CEST",
                "ORCCA | Status NCM",
                "NCM para cadastro",
                "CST IBS/CBS",
                "cClassTrib",
                "Descrição cClassTrib",
                "% Redução CBS",
                "% Redução IBS UF",
                "% Redução IBS Mun",
                "Imposto Seletivo (2027+)",
                "CFOP ECF/CFe",
                "CFOP NFe",
                "CST",
                "Natureza de operação",
                "Análise ORCCA",
            ]
        )
        aba.append(
            [
                10,
                "MOSTARDA",
                "07051900",
                "1702200",
                "Incorreto",
                "21033021",
                "000",
                "000001",
                "Tributação integral",
                0,
                0,
                0,
                "Não",
                "5102",
                "5405",
                "060",
                "5102 - Venda de mercadoria",
                "Mostarda preparada, fora do capítulo de hortaliças.",
            ]
        )
        buffer = BytesIO()
        livro.save(buffer)
        buffer.seek(0)
        item = ler_contabilidade(buffer)[0]
        self.assertEqual(item.ncm, "21033021")
        self.assertEqual(item.ncm_anterior, "07051900")
        self.assertEqual(item.cest, "1702200")
        self.assertEqual(item.cst_ibs_cbs, "000")
        self.assertEqual(item.descricao_cclass, "Tributação integral")
        self.assertEqual(item.reducao_cbs, "0")
        self.assertEqual(item.cfop_pdv, "5102")
        self.assertEqual(item.cfop_nfe, "5405")
        self.assertEqual(item.cst_icms, "060")
        self.assertEqual(item.natureza_codigo, "5102")
        self.assertEqual(item.natureza_descricao, "Venda de mercadoria")
        self.assertEqual(item.imposto_seletivo, "Não")
        self.assertEqual(item.analise, "Mostarda preparada, fora do capítulo de hortaliças.")


class AplicacaoTeste(unittest.TestCase):
    def test_atualiza_ncm_e_classificacao_e_preserva_o_resto(self):
        origem = csv_sistema(
            [
                "10|MOSTARDA|789123|07051900|1702200|000000|000000|000000",
                "11|AÇÚCAR|456|17019900|1700100|000001|000000|000001",
            ]
        )
        auditoria = planilha(
            [
                [99, "Mostarda", "11111111", "Incorreto", "2103.30.21", 1],
                [10, "TRIPAS", "05040013", "Correto", "05040013", "000001"],
            ]
        )
        resultado, _relatorio = processar(io.BytesIO(auditoria), origem)
        texto = resultado.csv_bytes.decode("latin-1")
        linhas = texto.split("\r\n")
        self.assertEqual(
            linhas[1],
            "10|MOSTARDA|789123|21033021|1702200|000001|000000|000001",
        )
        self.assertEqual(linhas[2], "11|AÇÚCAR|456|17019900|1700100|000001|000000|000001")
        self.assertEqual(resultado.resumo["aplicados"], 1)
        self.assertEqual(resultado.aplicados[0].ncm_anterior, "07051900")
        self.assertEqual(resultado.aplicados[0].ncm_novo, "21033021")
        self.assertEqual(resultado.aplicados[0].nfe_novo, "000001")
        self.assertEqual(resultado.aplicados[0].nfce_novo, "000001")
        self.assertEqual(resultado.resumo["sem_par"], 1)

    def test_csv_inalterado_quando_nao_ha_par(self):
        origem = csv_sistema(["1|ARROZ|1|10063021||000000|000000|000000"])
        auditoria = planilha([[1, "FEIJÃO", "07133399", "Correto", "07133399", "000001"]])
        resultado, _relatorio = processar(io.BytesIO(auditoria), origem)
        self.assertEqual(resultado.csv_bytes, origem)
        self.assertEqual(resultado.resumo["aplicados"], 0)
        self.assertEqual(resultado.resumo["sem_par"], 1)

    def test_status_de_revisao_e_nao_mercadoria_ficam_de_fora(self):
        origem = csv_sistema(["1|MOSTARDA|1|21033021||000000|000000|000000"])
        auditoria = planilha(
            [
                [1, "MOSTARDA", "21033021", "Revisar (análise do cliente)", "19019090", "000001"],
                [2, "MOSTARDA", "21033021", "Não é mercadoria", None, None],
            ]
        )
        resultado, _relatorio = processar(io.BytesIO(auditoria), origem)
        self.assertEqual(resultado.csv_bytes, origem)
        self.assertEqual(resultado.resumo["excluidos"], 2)
        self.assertEqual(resultado.resumo["aplicados"], 0)

    def test_descricao_repetida_nao_altera_o_csv(self):
        origem = csv_sistema(
            [
                "1|PATINHO|1|02013000||000000|000000|000000",
                "2|PATINHO|2|02013000||000000|000000|000000",
            ]
        )
        auditoria = planilha([[1, "PATINHO", "02021000", "Incorreto", "02013000", "200003"]])
        resultado, _relatorio = processar(io.BytesIO(auditoria), origem)
        self.assertEqual(resultado.csv_bytes, origem)
        self.assertEqual(resultado.resumo["ambiguos"], 1)
        self.assertEqual(len(resultado.ambiguos[0].candidatos), 2)

    def test_casamento_por_embalagem(self):
        origem = csv_sistema(
            ["3|SUKITA PET 200ML SH C/12|1|22021000||000000|000000|000000"]
        )
        auditoria = planilha([[8, "SUKITA PET 200ML", "22021000", "Correto", "22029900", "000001"]])
        resultado, _relatorio = processar(io.BytesIO(auditoria), origem)
        self.assertIn("22029900", resultado.csv_bytes.decode("latin-1"))
        self.assertEqual(resultado.aplicados[0].tipo_casamento, "embalagem")
        self.assertEqual(resultado.aplicados[0].identificador, "3")

    def test_campo_invalido_grava_somente_o_valido(self):
        origem = csv_sistema(["1|MOSTARDA|1|07051900||000000|000000|000000"])
        auditoria = planilha([[1, "MOSTARDA", "07051900", "Correto", "21033021", "ABC"]])
        resultado, relatorio = processar(io.BytesIO(auditoria), origem)
        linha = resultado.csv_bytes.decode("latin-1").split("\r\n")[1]
        self.assertEqual(linha, "1|MOSTARDA|1|21033021||000000|000000|000000")
        self.assertIn("cClassTrib inválido", resultado.aplicados[0].avisos)
        livro = openpyxl.load_workbook(io.BytesIO(relatorio))
        self.assertEqual(livro.sheetnames[0], "Resumo")
        self.assertIn("Aplicados", livro.sheetnames)

    def test_ncm_e_cclass_vazios_nao_gravam(self):
        origem = csv_sistema(["1|MOSTARDA|1|21033021||000000|000000|000000"])
        auditoria = planilha([[1, "MOSTARDA", "21033021", "Correto", None, None]])
        resultado, _relatorio = processar(io.BytesIO(auditoria), origem)
        self.assertEqual(resultado.csv_bytes, origem)
        self.assertEqual(resultado.resumo["invalidos"], 1)
        self.assertEqual(resultado.resumo["aplicados"], 0)


class PaginaTeste(unittest.TestCase):
    def test_envio_gera_csv_e_relatorio(self):
        servidor = criar_servidor(0)
        porta = servidor.server_address[1]
        fio = threading.Thread(target=servidor.serve_forever, daemon=True)
        fio.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{porta}/") as resposta:
                pagina = resposta.read().decode("utf-8")
            self.assertIn("Ajuste fiscal", pagina)
            self.assertIn('name="contabilidade"', pagina)

            origem = csv_sistema(["10|MOSTARDA|789|07051900||000000|000000|000000"])
            auditoria = planilha([[1, "MOSTARDA", "07051900", "Incorreto", "21033021", "000001"]])
            limite = "----AjusteFiscalTeste"
            corpo = b"".join(
                [
                    (
                        f"--{limite}\r\n"
                        'Content-Disposition: form-data; name="contabilidade"; filename="auditoria.xlsx"\r\n'
                        "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
                    ).encode("utf-8")
                    + auditoria
                    + b"\r\n",
                    (
                        f"--{limite}\r\n"
                        'Content-Disposition: form-data; name="sistema"; filename="Estoque.csv"\r\n'
                        "Content-Type: text/csv\r\n\r\n"
                    ).encode("utf-8")
                    + origem
                    + b"\r\n",
                    f"--{limite}--\r\n".encode("utf-8"),
                ]
            )
            pedido = urllib.request.Request(
                f"http://127.0.0.1:{porta}/processar",
                data=corpo,
                method="POST",
            )
            pedido.add_header("Content-Type", f"multipart/form-data; boundary={limite}")
            with urllib.request.urlopen(pedido) as resposta:
                pagina = resposta.read().decode("utf-8")
                endereco = resposta.geturl()
            self.assertIn("Aplicados", pagina)
            self.assertIn("21033021", pagina)
            self.assertIn("id=", endereco)
            with urllib.request.urlopen(endereco.replace("/?", "/baixar/csv?")) as resposta:
                baixado = resposta.read()
            self.assertIn(b"21033021", baixado)
            self.assertIn("789".encode("latin-1"), baixado)
            with urllib.request.urlopen(endereco.replace("/?", "/baixar/relatorio?")) as resposta:
                relatorio = resposta.read()
            self.assertTrue(relatorio.startswith(b"PK"))
        finally:
            servidor.shutdown()
            servidor.server_close()


ANEXO = """
7. Anexo I — Lista completa de produtos
# Código Descrição NCM
informado
NCM sugerido Status CST cClassTrib % Red.
CBS/IBS
IS CNAE Ação
1 10 MOSTARDA 0705.19.00 2103.30.21 Incorreto 000 000001 0 / 0 Não Aderente Atualizar NCM
2 11 AÇÚCAR 1701.99.00 — Correto 200 200003 100 / 100 Não Aderente Nenhuma
3 12 TORRESMO 0203.11.00 0209.10.11 Revisar 200 200003 100 / 100 Não Aderente Análise do cliente
4 13 ZZ TOUCA
6505.00.90 — Não é
mercadoria
— Não Aderente Revisar cadastro (não é mercadoria)
ORCCA CONTABILIDADE Auditoria de Produtos
Documento confidencial · emitido em 20/11/2026 Página 2
# Código Descrição NCM
informado
5 14 SM ESPONJA UN (60) 764 6805.30.90 — Correto 000 000001 0 / 0 Não Aderente Nenhuma
6 15 COSTELA BOVINA 0201.30.00 0201.20.90 Incorreto 200 200003 100 / 100 Não Aderente Atualizar NCM
8. Fundamentação legal
1 99 LINHA FORA DO ANEXO 0201.30.00 — Correto 200 200003 100 / 100 Não Aderente Nenhuma
"""


def pdf_simples(texto: str) -> bytes:
    comandos = [b"BT /F1 10 Tf 36 800 Td 12 TL"]
    for linha in texto.splitlines():
        cru = linha.encode("latin-1", errors="replace")
        cru = cru.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
        comandos.append(b"(" + cru + b") Tj T*")
    comandos.append(b"ET")
    fluxo = b"\n".join(comandos)
    objetos = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 840] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n",
        b"<< /Length " + str(len(fluxo)).encode() + b" >>\nstream\n" + fluxo + b"\nendstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>\nendobj\n",
    ]
    objetos[3] = b"4 0 obj\n" + objetos[3]
    saida = b"%PDF-1.4\n"
    offsets = [0]
    for blob in objetos:
        offsets.append(len(saida))
        saida += blob
    xref = len(saida)
    saida += f"xref\n0 {len(offsets)}\n".encode()
    saida += b"0000000000 65535 f \n"
    for deslocamento in offsets[1:]:
        saida += f"{deslocamento:010d} 00000 n \n".encode()
    saida += (
        f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    )
    return saida


class LaudoTeste(unittest.TestCase):
    def test_anexo_separa_status_ncm_e_descricao(self):
        itens = itens_do_texto(ANEXO)
        por_codigo = {item.codigo: item for item in itens}
        self.assertEqual(list(por_codigo), ["10", "11", "12", "13", "14", "15"])
        self.assertEqual(por_codigo["10"].ncm, "21033021")
        self.assertEqual(por_codigo["10"].ncm_anterior, "07051900")
        self.assertEqual(por_codigo["10"].cclass, "000001")
        self.assertEqual(por_codigo["10"].cst_ibs_cbs, "000")
        self.assertEqual(por_codigo["10"].reducao_cbs, "0")
        self.assertEqual(por_codigo["10"].imposto_seletivo, "Não")
        self.assertTrue(por_codigo["10"].definitivo)
        self.assertEqual(por_codigo["11"].descricao, "AÇÚCAR")
        self.assertEqual(por_codigo["11"].ncm, "17019900")
        self.assertEqual(por_codigo["11"].cclass, "200003")
        self.assertFalse(por_codigo["12"].definitivo)
        self.assertEqual(por_codigo["12"].ncm, "02091011")
        self.assertEqual(por_codigo["13"].descricao, "ZZ TOUCA")
        self.assertIsNone(por_codigo["13"].ncm)
        self.assertFalse(por_codigo["13"].definitivo)
        self.assertEqual(por_codigo["14"].descricao, "SM ESPONJA UN (60) 764")
        self.assertEqual(por_codigo["14"].ncm, "68053090")
        self.assertEqual(por_codigo["15"].ncm, "02012090")
        self.assertNotIn("99", por_codigo)

    def test_pdf_entra_no_mesmo_ajuste_do_csv(self):
        origem = csv_sistema(["10|MOSTARDA|789|07051900||000000|000000|000000"])
        resultado, _relatorio = processar(io.BytesIO(pdf_simples(ANEXO)), origem)
        linha = resultado.csv_bytes.decode("latin-1").split("\r\n")[1]
        self.assertEqual(linha, "10|MOSTARDA|789|21033021||000001|000000|000001")
        self.assertEqual(resultado.resumo["excluidos"], 2)

    def test_laudo_real_bate_com_a_planilha(self):
        pdf = Path(r"C:\Users\Gomes\Downloads\Laudo_SaoCarlos.pdf")
        xlsx = Path(r"C:\Users\Gomes\Downloads\Planilha_SaoCarlos.xlsx")
        if not pdf.exists() or not xlsx.exists():
            self.skipTest("arquivos de exemplo ausentes")
        do_pdf = ler_laudo(pdf.read_bytes())
        do_xlsx = ler_origem(xlsx.read_bytes())
        self.assertEqual(len(do_pdf), 886)
        por_planilha = {item.codigo: item for item in do_xlsx}
        for item in do_pdf:
            outro = por_planilha[item.codigo]
            self.assertEqual(item.definitivo, outro.definitivo, item.codigo)
            if item.definitivo or item.status == "Revisar":
                self.assertEqual(item.ncm, outro.ncm, item.codigo)
                self.assertEqual(item.cclass, outro.cclass, item.codigo)
                self.assertEqual(item.descricao_norm, outro.descricao_norm, item.codigo)


if __name__ == "__main__":
    unittest.main()
