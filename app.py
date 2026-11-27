"""Página local para aplicar o ajuste fiscal no CSV de importação do sistema."""

import html
import json
import re
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from ajuste_fiscal import importar_para_base, processar
from ajuste_fiscal.banco import (
    aceitar_sugestao,
    consultar_ean,
    consultar_lote,
    criar_parceiro,
    gravar_configuracao,
    SITUACOES_CADASTRO,
    listar_cadastros,
    listar_produtos,
    listar_sugestoes_pendentes,
    recusar_sugestao,
)
from ajuste_fiscal.classificar import conferir, estado_chave_gemini, sugerir_lote, sugerir_publico
from ajuste_fiscal.saneamento import SemToken, sanear
from ajuste_fiscal.sanitizar import sanitizar_csv
from ajuste_fiscal.tabelas import sincronizar

HOST = "0.0.0.0"
PORTA = 8765
LIMITE_BYTES = 32 * 1024 * 1024
MAX_RESULTADOS = 20
RESULTADOS = {}
ACOES = {}


def main() -> None:
    servidor = criar_servidor(PORTA)
    print(f"Ajuste fiscal em http://{HOST}:{PORTA}")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
    finally:
        servidor.server_close()


def criar_servidor(porta: int = PORTA) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((HOST, porta), Pedido)


class Pedido(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        caminho = urlparse(self.path)
        if caminho.path == "/favicon.ico":
            self.send_error(404)
            return
        ean = re.fullmatch(r"/v1/ean/([^/]+)", caminho.path)
        if ean:
            self._ean(unquote(ean.group(1)))
            return
        if caminho.path in {"/baixar/csv", "/baixar/relatorio"}:
            self._baixar(caminho)
            return
        consulta = parse_qs(caminho.query)
        if caminho.path == "/v1/produtos":
            self._produtos(consulta)
            return
        if caminho.path != "/":
            self.send_error(404)
            return
        token = consulta.get("id", [None])[0]
        pacote = RESULTADOS.get(token) if token else None
        if token and pacote is None:
            self._html(renderizar(erro="Este resultado expirou. Envie os arquivos de novo."))
            return
        acao_token = consulta.get("acao", [None])[0]
        acao = ACOES.get(acao_token)
        busca = consulta.get("q", [""])[0]
        try:
            numero_pagina = int(consulta.get("pagina", ["1"])[0])
        except ValueError:
            numero_pagina = 1
        sugestoes, aviso_base = _pendencias()
        try:
            lista = listar_produtos(busca, numero_pagina)
            aviso_lista = None
        except Exception:
            lista = {"itens": [], "total": 0, "pagina": 1, "paginas": 1, "busca": busca}
            aviso_lista = "A base fiscal está indisponível."
        situacao = consulta.get("situacao", ["sanitizado"])[0]
        try:
            cadastros = listar_cadastros(situacao, numero_pagina)
            aviso_cadastros = None
        except Exception:
            cadastros = _cadastros_vazios(situacao)
            aviso_cadastros = "A base fiscal está indisponível."
        aba = _aba_ativa(consulta.get("aba", [None])[0], pacote, acao, busca)
        self._html(
            renderizar(
                pacote=pacote,
                token=token,
                sugestoes=sugestoes,
                aviso_base=aviso_base,
                resumo_base=acao,
                lista=lista,
                aviso_lista=aviso_lista,
                cadastros=cadastros,
                aviso_cadastros=aviso_cadastros,
                aba=aba,
                acao_token=acao_token,
                chave=estado_chave_gemini(),
            )
        )

    def do_POST(self) -> None:
        caminho = urlparse(self.path).path
        if caminho == "/v1/saneamento":
            self._saneamento()
            return
        if caminho == "/v1/consultar":
            self._consultar_lote()
            return
        if caminho == "/v1/sugerir/lote":
            self._sugerir_lote()
            return
        if caminho == "/v1/sugerir":
            self._sugerir()
            return
        if caminho == "/parceiros":
            self._parceiro()
            return
        if caminho == "/configuracao":
            self._configuracao()
            return
        if caminho == "/tabelas/atualizar":
            self._redirecionar_acao(sincronizar().como_dict())
            return
        if caminho == "/cadastros/conferir":
            self._redirecionar_acao(conferir().como_dict())
            return
        if caminho in {"/sugestoes/aceitar", "/sugestoes/recusar"}:
            self._decidir_sugestao(caminho.endswith("/aceitar"))
            return
        if caminho == "/importar":
            self._importar()
            return
        if caminho != "/processar":
            self.send_error(404)
            return
        tamanho = int(self.headers.get("Content-Length", "0") or "0")
        if tamanho <= 0 or tamanho > LIMITE_BYTES:
            self._html(renderizar(erro="Envie o laudo ou a planilha da contabilidade e o CSV do sistema."))
            return
        corpo = self.rfile.read(tamanho)
        try:
            arquivos = ler_multipart(self.headers.get("Content-Type", ""), corpo)
            contabilidade = arquivos.get("contabilidade")
            sistema = arquivos.get("sistema")
            if not contabilidade or not contabilidade[1]:
                raise ValueError("Escolha a planilha da contabilidade (.xlsx) ou o laudo em PDF.")
            if not sistema or not sistema[1]:
                raise ValueError("Escolha o CSV exportado pelo sistema.")
            from io import BytesIO

            resultado, relatorio = processar(
                BytesIO(contabilidade[1]),
                sistema[1],
                nome_arquivo=contabilidade[0],
            )
        except ValueError as exc:
            self._html(renderizar(erro=str(exc)))
            return
        except Exception:
            traceback.print_exc()
            self._html(
                renderizar(
                    erro="Falha ao ler os arquivos. Use o laudo em PDF ou a planilha com a aba Produtos auditados, e o CSV exportado pelo sistema."
                )
            )
            return
        token = uuid.uuid4().hex
        RESULTADOS[token] = {
            "csv": resultado.csv_bytes,
            "relatorio": relatorio,
            "resumo": resultado.resumo,
            "amostra": resultado.aplicados[:20],
            "total_aplicados": len(resultado.aplicados),
            "charset": "utf-8" if resultado.planilha.encoding.startswith("utf-8") else "latin-1",
            "coleta": resultado.coleta.como_dict() if resultado.coleta else None,
        }
        while len(RESULTADOS) > MAX_RESULTADOS:
            RESULTADOS.pop(next(iter(RESULTADOS)))
        self.send_response(303)
        self.send_header("Location", f"/?id={token}&aba=ajuste")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _baixar(self, caminho) -> None:
        token = parse_qs(caminho.query).get("id", [None])[0]
        pacote = RESULTADOS.get(token) if token else None
        if pacote is None:
            self.send_error(404)
            return
        if caminho.path.endswith("/csv"):
            conteudo = pacote["csv"]
            tipo = f"text/csv; charset={pacote['charset']}"
            nome = "estoque-sanitizado.csv" if pacote.get("tipo") == "sanitizacao" else "estoque-ajustado.csv"
        else:
            conteudo = pacote["relatorio"]
            tipo = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            nome = "relatorio-ajuste.xlsx"
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Disposition", f'attachment; filename="{nome}"')
        self.send_header("Content-Length", str(len(conteudo)))
        self.end_headers()
        self.wfile.write(conteudo)

    def _importar(self) -> None:
        tamanho = int(self.headers.get("Content-Length", "0") or "0")
        if tamanho <= 0 or tamanho > LIMITE_BYTES:
            self._html(renderizar(erro="Escolha a planilha, o laudo ou o CSV do sistema.", aba="importar"))
            return
        try:
            arquivos = ler_multipart(self.headers.get("Content-Type", ""), self.rfile.read(tamanho))
            sistema = arquivos.get("sistema")
            if sistema and sistema[1]:
                self._sanitizar(sistema)
                return
            arquivo = arquivos.get("produtos")
            if not arquivo or not arquivo[1]:
                raise ValueError("Escolha a planilha com a aba Importacao, o laudo em PDF ou o CSV do sistema.")
            from io import BytesIO

            resumo = importar_para_base(BytesIO(arquivo[1]), nome_arquivo=arquivo[0])
        except ValueError as exc:
            self._html(renderizar(erro=str(exc), aba="importar"))
            return
        except Exception:
            traceback.print_exc()
            self._html(renderizar(erro="Falha ao importar os produtos para a base.", aba="importar"))
            return
        self._redirecionar_acao(resumo.como_dict(), "importar")

    def _sanitizar(self, arquivo: tuple[str, bytes]) -> None:
        try:
            resultado = sanitizar_csv(arquivo[1], arquivo[0])
        except ValueError as exc:
            self._html(renderizar(erro=str(exc), aba="importar"))
            return
        except Exception:
            traceback.print_exc()
            self._html(renderizar(erro="Falha ao sanitizar o CSV do sistema.", aba="importar"))
            return
        token = uuid.uuid4().hex
        RESULTADOS[token] = {
            "tipo": "sanitizacao",
            "csv": resultado.csv_bytes,
            "charset": resultado.charset,
        }
        while len(RESULTADOS) > MAX_RESULTADOS:
            RESULTADOS.pop(next(iter(RESULTADOS)))
        self.send_response(303)
        self.send_header("Location", f"/?id={token}&aba=importar&situacao=sanitizado")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _sugerir(self) -> None:
        tamanho = int(self.headers.get("Content-Length", "0") or "0")
        if tamanho <= 0 or tamanho > LIMITE_BYTES:
            self._json(400, {"erro": "Informe a descrição do produto."})
            return
        try:
            pedido = json.loads(self.rfile.read(tamanho).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"erro": "Informe a descrição do produto."})
            return
        if not isinstance(pedido, dict):
            self._json(400, {"erro": "Informe a descrição do produto."})
            return
        status, corpo = sugerir_publico(
            str(pedido.get("descricao") or ""),
            pedido.get("ncm_atual"),
            pedido.get("ean"),
        )
        self._json(status, corpo)

    def _corpo_json(self) -> dict | None:
        tamanho = int(self.headers.get("Content-Length", "0") or "0")
        if tamanho <= 0 or tamanho > LIMITE_BYTES:
            return None
        try:
            pedido = json.loads(self.rfile.read(tamanho).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(pedido, dict):
            return None
        return pedido

    def _consultar_lote(self) -> None:
        pedido = self._corpo_json()
        if pedido is None:
            self._json(400, {"erro": "Informe os EANs ou as descrições do lote."})
            return
        eans = pedido.get("eans") or []
        descricoes = pedido.get("descricoes") or []
        if isinstance(eans, str):
            eans = [eans]
        if isinstance(descricoes, str):
            descricoes = [descricoes]
        if not isinstance(eans, list) or not isinstance(descricoes, list):
            self._json(400, {"erro": "Informe os EANs ou as descrições do lote."})
            return
        try:
            corpo = consultar_lote(eans, descricoes, completo=bool(pedido.get("completo")))
        except ValueError as exc:
            self._json(400, {"erro": str(exc)})
            return
        except Exception:
            traceback.print_exc()
            self._json(503, {"erro": "A base fiscal está indisponível."})
            return
        self._json(200, corpo)

    def _saneamento(self) -> None:
        pedido = self._corpo_json()
        if pedido is None:
            self._json(400, {"erro": "Informe o emitente, o perfil e os produtos."})
            return
        autorizacao = self.headers.get("Authorization", "")
        token = autorizacao[7:].strip() if autorizacao.lower().startswith("bearer ") else ""
        try:
            self._json(200, sanear(pedido, token))
        except SemToken:
            self._json(401, {"erro": "Token ausente ou inválido."})
        except ValueError as exc:
            self._json(400, {"erro": str(exc)})
        except Exception:
            traceback.print_exc()
            self._json(503, {"erro": "A base fiscal está indisponível."})

    def _configuracao(self) -> None:
        tamanho = int(self.headers.get("Content-Length", "0") or "0")
        corpo = self.rfile.read(tamanho).decode("utf-8", errors="replace") if tamanho else ""
        campos = parse_qs(corpo)
        if campos.get("remover", ["0"])[0] == "1":
            valor = ""
            aviso = "removida"
        else:
            valor = campos.get("gemini_api_key", [""])[0].strip()
            if not valor:
                self._html(
                    renderizar(
                        erro="Cole a chave da API do Gemini.",
                        aba="configuracao",
                        chave=estado_chave_gemini(),
                        **_contexto_base(),
                    )
                )
                return
            aviso = "salva"
        try:
            gravar_configuracao("gemini_api_key", valor)
        except Exception:
            traceback.print_exc()
            self._html(
                renderizar(
                    erro="A base fiscal está indisponível.",
                    aba="configuracao",
                    chave={"origem": None, "sufixo": ""},
                    **_contexto_base(),
                )
            )
            return
        self._redirecionar_acao({"configuracao": aviso}, "configuracao")

    def _parceiro(self) -> None:
        tamanho = int(self.headers.get("Content-Length", "0") or "0")
        corpo = self.rfile.read(tamanho).decode("utf-8", errors="replace") if tamanho else ""
        campos = parse_qs(corpo)
        try:
            parceiro = criar_parceiro(campos.get("nome", [""])[0], campos.get("cnpj", [""])[0])
        except ValueError as exc:
            self._html(renderizar(erro=str(exc), aba="base", **_contexto_base()))
            return
        except Exception:
            traceback.print_exc()
            self._html(renderizar(erro="A base fiscal está indisponível.", aba="base", **_contexto_base()))
            return
        self._redirecionar_acao(parceiro, "base")

    def _sugerir_lote(self) -> None:
        pedido = self._corpo_json()
        if pedido is None or not isinstance(pedido.get("itens"), list):
            self._json(400, {"erro": "Informe ao menos um produto."})
            return
        status, corpo = sugerir_lote(pedido["itens"])
        self._json(status, corpo)

    def _decidir_sugestao(self, aceitar: bool) -> None:
        tamanho = int(self.headers.get("Content-Length", "0") or "0")
        corpo = self.rfile.read(tamanho).decode("utf-8", errors="replace") if tamanho else ""
        identificador = parse_qs(corpo).get("id", [""])[0]
        if not identificador.isdigit():
            self._html(renderizar(erro="Sugestão inválida.", **_contexto_base()))
            return
        ok = aceitar_sugestao(int(identificador)) if aceitar else recusar_sugestao(int(identificador))
        if not ok:
            self._html(renderizar(erro="Não foi possível atualizar a sugestão.", **_contexto_base()))
            return
        self._redirecionar_acao({"decisao": "aceita" if aceitar else "recusada"})

    def _produtos(self, consulta: dict) -> None:
        busca = consulta.get("q", [""])[0]
        try:
            numero_pagina = int(consulta.get("pagina", ["1"])[0])
        except ValueError:
            numero_pagina = 1
        try:
            self._json(200, listar_produtos(busca, numero_pagina))
        except Exception:
            traceback.print_exc()
            self._json(503, {"erro": "A base fiscal está indisponível."})

    def _redirecionar_acao(self, resumo: dict, aba: str = "base") -> None:
        token = uuid.uuid4().hex
        ACOES[token] = resumo
        while len(ACOES) > MAX_RESULTADOS:
            ACOES.pop(next(iter(ACOES)))
        self.send_response(303)
        self.send_header("Location", f"/?acao={token}&aba={aba}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, status: int, corpo: dict) -> None:
        dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def _ean(self, ean: str) -> None:
        try:
            status, corpo = consultar_ean(ean)
        except Exception:
            traceback.print_exc()
            status, corpo = 503, {"erro": "A base fiscal está indisponível."}
        dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def _html(self, corpo: str) -> None:
        dados = corpo.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def log_message(self, formato, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), formato % args))


def ler_multipart(content_type: str, corpo: bytes) -> dict[str, tuple[str, bytes]]:
    encontrado = re.search(r"boundary=(?P<valor>[^;]+)", content_type or "", flags=re.IGNORECASE)
    if encontrado is None:
        raise ValueError("Envie os dois arquivos pelo formulário.")
    limite = encontrado.group("valor").strip().strip('"').encode("utf-8", errors="strict")
    arquivos = {}
    for pedaco in corpo.split(b"--" + limite):
        if pedaco.startswith(b"--") or pedaco in {b"", b"\r\n", b"\n"}:
            continue
        if pedaco.startswith(b"\r\n"):
            pedaco = pedaco[2:]
        elif pedaco.startswith(b"\n"):
            pedaco = pedaco[1:]
        if pedaco.endswith(b"\r\n"):
            pedaco = pedaco[:-2]
        elif pedaco.endswith(b"\n"):
            pedaco = pedaco[:-1]
        separador = b"\r\n\r\n" if b"\r\n\r\n" in pedaco else b"\n\n"
        cabecalho, _, dados = pedaco.partition(separador)
        if not cabecalho:
            continue
        texto = cabecalho.decode("utf-8", errors="replace")
        nome = _parametro(texto, "name")
        if nome:
            arquivos[nome] = (_parametro(texto, "filename") or "", dados)
    return arquivos


def _parametro(cabecalho: str, chave: str) -> str:
    encontrado = re.search(
        rf'{chave}="(?P<valor>[^"]*)"|{chave}=(?P<simples>[^;\r\n]+)',
        cabecalho,
        flags=re.IGNORECASE,
    )
    if encontrado is None:
        return ""
    return (encontrado.group("valor") or encontrado.group("simples") or "").strip()


def renderizar(
    erro: str | None = None,
    pacote: dict | None = None,
    token: str | None = None,
    sugestoes: list | None = None,
    aviso_base: str | None = None,
    resumo_base: dict | None = None,
    lista: dict | None = None,
    aviso_lista: str | None = None,
    cadastros: dict | None = None,
    aviso_cadastros: str | None = None,
    aba: str = "ajuste",
    acao_token: str | None = None,
    chave: dict | None = None,
) -> str:
    aviso = f'<p class="erro">{html.escape(erro)}</p>' if erro else ""
    resultado = _resultado(pacote, token) if pacote and pacote.get("tipo") != "sanitizacao" else ""
    base = _base(sugestoes or [], aviso_base, resumo_base)
    catalogo = _catalogo(lista or {"itens": [], "total": 0, "pagina": 1, "paginas": 1, "busca": ""}, aviso_lista)
    sanitizados = _sanitizados(cadastros or _cadastros_vazios(), aviso_cadastros, pacote, token, acao_token)
    abas = _abas(aba, token, acao_token)
    configuracao = _configuracoes(chave or {}, resumo_base)
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ajuste fiscal</title>
  <style>
    :root {{
      --papel: #f3efe6;
      --tinta: #1c1915;
      --muted: #5c564c;
      --card: #fffdf8;
      --linha: #e2dacb;
      --verde: #0f6b4c;
      --verde-escuro: #0b5139;
      --alerta: #8a3d12;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", Candara, sans-serif;
      color: var(--tinta);
      background: var(--papel);
    }}
    main {{
      max-width: 1080px;
      margin: 0 auto;
      padding: 40px 20px 64px;
    }}
    h1 {{
      font-size: 2rem;
      font-weight: 650;
      margin: 0 0 8px;
      letter-spacing: -0.03em;
    }}
    .intro {{
      max-width: 68ch;
      color: var(--muted);
      line-height: 1.5;
      margin-bottom: 28px;
    }}
    form, .painel {{
      background: var(--card);
      border: 1px solid var(--linha);
      border-radius: 16px;
      padding: 22px;
    }}
    .painel {{ overflow-x: auto; }}
    .campos {{
      display: grid;
      gap: 16px;
    }}
    @media (min-width: 760px) {{
      .campos {{ grid-template-columns: 1fr 1fr; }}
    }}
    label {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      font-weight: 650;
    }}
    label > span:not(.escolha), .nome {{
      font-weight: 450;
      color: var(--muted);
    }}
    .botao {{
      color: var(--tinta);
    }}
    .escolha {{
      position: relative;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px;
      border: 1px dashed #c9bfae;
      border-radius: 10px;
      background: #faf7f1;
      font-weight: 450;
    }}
    .escolha input {{
      position: absolute;
      width: 1px;
      height: 1px;
      opacity: 0;
    }}
    .botao {{
      border: 1px solid #c9bfae;
      background: white;
      border-radius: 8px;
      padding: 8px 12px;
      font-weight: 650;
      white-space: nowrap;
    }}
    .nome {{ color: var(--muted); overflow: hidden; text-overflow: ellipsis; }}
    button {{
      margin-top: 18px;
      background: var(--verde);
      color: white;
      border: 0;
      border-radius: 999px;
      padding: 12px 22px;
      font: inherit;
      font-weight: 650;
      cursor: pointer;
    }}
    button:hover {{ background: var(--verde-escuro); }}
    .erro {{
      background: #fff1e8;
      color: var(--alerta);
      border-radius: 12px;
      padding: 12px 14px;
    }}
    .contagens {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 10px;
      margin: 22px 0;
    }}
    .contagem {{
      background: var(--card);
      border: 1px solid var(--linha);
      border-radius: 14px;
      padding: 14px;
    }}
    .contagem strong {{
      display: block;
      font-size: 1.6rem;
      letter-spacing: -0.04em;
    }}
    .contagem span {{ color: var(--muted); font-size: 0.92rem; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.92rem;
    }}
    th, td {{
      text-align: left;
      padding: 8px 10px;
      border-bottom: 1px solid var(--linha);
      vertical-align: top;
    }}
    th {{ color: var(--muted); font-weight: 650; }}
    code {{ font-family: Consolas, "Courier New", monospace; white-space: nowrap; }}
    .acoes {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 8px 0 18px; }}
    .acoes a {{
      background: var(--verde);
      color: white;
      text-decoration: none;
      border-radius: 999px;
      padding: 10px 16px;
      font-weight: 650;
    }}
    .acoes a.secundario {{ background: #3d3832; }}
    .nota {{ color: var(--muted); line-height: 1.5; }}
    form.consulta {{
      margin-top: 16px;
      display: flex;
      gap: 12px;
      align-items: end;
    }}
    form.consulta input {{
      padding: 12px;
      border: 1px solid #c9bfae;
      border-radius: 10px;
      font: inherit;
    }}
    form.consulta button {{ margin-top: 0; }}
    form.linha {{ display: inline; }}
    form.linha button, .acoes button {{
      margin-top: 0;
      background: var(--verde);
      color: white;
      border: 0;
      border-radius: 999px;
      padding: 8px 14px;
      font: inherit;
      font-weight: 650;
      cursor: pointer;
    }}
    form.linha button.secundario {{ background: #3d3832; }}
    .abas {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 18px;
    }}
    .abas a {{
      color: var(--tinta);
      text-decoration: none;
      border: 1px solid var(--linha);
      background: var(--card);
      border-radius: 999px;
      padding: 8px 14px;
      font-weight: 650;
    }}
    .abas a.ativa {{
      background: var(--verde);
      color: white;
      border-color: var(--verde);
    }}
    .aba {{ display: none; }}
    .aba.ativa {{ display: block; }}
    .aba > form + form {{ margin-top: 16px; }}
    form.busca {{
      display: flex;
      gap: 12px;
      align-items: end;
      margin-bottom: 16px;
    }}
    form.busca input {{
      flex: 1;
      padding: 12px;
      border: 1px solid #c9bfae;
      border-radius: 10px;
      font: inherit;
    }}
    form.busca button {{ margin-top: 0; }}
    form[action="/parceiros"] {{ margin-top: 16px; }}
    form[action="/configuracao"] input,
    form[action="/parceiros"] input {{
      padding: 12px;
      border: 1px solid #c9bfae;
      border-radius: 10px;
      font: inherit;
    }}
    .paginas {{
      display: flex;
      gap: 12px;
      align-items: center;
      margin-top: 14px;
    }}
    .paginas a {{
      color: var(--verde-escuro);
      font-weight: 650;
    }}
    .filtros {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 8px 0 16px;
    }}
    .filtros a {{
      color: var(--tinta);
      text-decoration: none;
      border: 1px solid var(--linha);
      background: var(--card);
      border-radius: 999px;
      padding: 8px 14px;
      font-weight: 650;
    }}
    .filtros a.ativa {{
      background: var(--verde);
      color: white;
      border-color: var(--verde);
    }}
    tr.sanitizado td {{ background: #f3faf6; }}
    .carregando {{
      position: fixed;
      inset: 0;
      z-index: 20;
      display: flex;
      align-items: center;
      justify-content: center;
      background: rgba(28, 25, 21, 0.45);
      padding: 20px;
    }}
    .carregando[hidden] {{ display: none !important; }}
    .carregando .cartao {{
      width: min(420px, 100%);
      background: var(--card);
      border-radius: 16px;
      padding: 28px 32px;
      text-align: center;
    }}
    .carregando strong {{
      display: block;
      font-size: 1.25rem;
      margin-bottom: 6px;
    }}
    .carregando p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.45;
    }}
    .giro {{
      width: 36px;
      height: 36px;
      margin: 0 auto 14px;
      border: 3px solid var(--linha);
      border-top-color: var(--verde);
      border-radius: 50%;
      animation: girar 0.8s linear infinite;
    }}
    @keyframes girar {{
      to {{ transform: rotate(360deg); }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>Ajuste fiscal</h1>
    <p class="intro">Gera o CSV de importação com o NCM para cadastro e o cClassTrib dos produtos com cadastro definitivo (Correto e Incorreto). A origem pode ser a planilha .xlsx ou o laudo em PDF: o Anexo I é lido pelo texto do arquivo, sem interpretação por IA. Revisar e Não é mercadoria ficam só no relatório. Código de barras, CFOP, ICMS, PIS, COFINS e a classificação regular permanecem como estão.</p>
    {aviso}
    {abas}
    <section class="aba{' ativa' if aba == 'ajuste' else ''}">
    <form action="/processar" method="post" enctype="multipart/form-data" data-carregando="Gerando o CSV de importação…">
      <div class="campos">
        <label>Planilha ou laudo
          <span>Arquivo .xlsx com a aba Produtos auditados, ou o laudo em PDF.</span>
          <span class="escolha">
            <input type="file" name="contabilidade" accept=".xlsx,.pdf,application/pdf,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" required onchange="nomeArquivo(this)">
            <span class="botao">Escolher arquivo</span>
            <span class="nome">Nenhum arquivo escolhido</span>
          </span>
        </label>
        <label>CSV do sistema
          <span>Exportação que o sistema também importa, separada por |.</span>
          <span class="escolha">
            <input type="file" name="sistema" accept=".csv,text/csv" required onchange="nomeArquivo(this)">
            <span class="botao">Escolher arquivo</span>
            <span class="nome">Nenhum arquivo escolhido</span>
          </span>
        </label>
      </div>
      <button type="submit">Gerar CSV de importação</button>
    </form>
    {resultado}
    </section>
    <section class="aba{' ativa' if aba == 'importar' else ''}">
    <form action="/importar" method="post" enctype="multipart/form-data" data-carregando="Importando os produtos para a base…">
      <div class="campos">
        <label>Importar para a base
          <span>Aba Importacao, aba Produtos auditados ou laudo em PDF. Entram só Correto e Incorreto.</span>
          <span class="escolha">
            <input type="file" name="produtos" accept=".xlsx,.pdf,application/pdf,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" required onchange="nomeArquivo(this)">
            <span class="botao">Escolher arquivo</span>
            <span class="nome">Nenhum arquivo escolhido</span>
          </span>
        </label>
      </div>
      <button type="submit">Importar produtos</button>
    </form>
    <form action="/importar" method="post" enctype="multipart/form-data" data-carregando="Lendo o CSV e sanitizando os cadastros…">
      <div class="campos">
        <label>CSV do sistema
          <span>Exportação separada por |. O NCM, o CEST e o cClassTrib confirmados na base voltam para o arquivo. EAN, CFOP e CST que a base ainda não tinha sobem para o cadastro. Quem não está na base fica na fila de conferência.</span>
          <span class="escolha">
            <input type="file" name="sistema" accept=".csv,text/csv" required onchange="nomeArquivo(this)">
            <span class="botao">Escolher arquivo</span>
            <span class="nome">Nenhum arquivo escolhido</span>
          </span>
        </label>
      </div>
      <button type="submit">Sanitizar cadastros</button>
    </form>
    {sanitizados}
    </section>
    <section class="aba{' ativa' if aba == 'produtos' else ''}">
    {catalogo}
    </section>
    <section class="aba{' ativa' if aba == 'consulta' else ''}">
    <form class="consulta" onsubmit="abrirEan(event)">
      <label>Consultar EAN
        <span>Um EAN em /v1/ean/código. Vários de uma vez em POST /v1/consultar, com resposta curta para gastar menos tokens. A classificação em lote fica em POST /v1/sugerir/lote.</span>
        <input name="ean" inputmode="numeric" placeholder="7891234567895" required>
      </label>
      <button type="submit">Consultar</button>
    </form>
    </section>
    <section class="aba{' ativa' if aba == 'base' else ''}">
    {base}
    </section>
    <section class="aba{' ativa' if aba == 'configuracao' else ''}">
    {configuracao}
    </section>
  </main>
  <div id="carregando" class="carregando" hidden>
    <div class="cartao" role="status" aria-live="polite">
      <div class="giro"></div>
      <strong>Aguarde</strong>
      <p id="carregando-texto">Processando…</p>
    </div>
  </div>
  <script>
    function nomeArquivo(input) {{
      var nome = input.parentElement.querySelector(".nome");
      nome.textContent = input.files.length ? input.files[0].name : "Nenhum arquivo escolhido";
    }}
    document.querySelectorAll("form").forEach(function (form) {{
      form.addEventListener("submit", function () {{
        if ((form.getAttribute("method") || "").toLowerCase() !== "post") return;
        var painel = document.getElementById("carregando");
        var texto = document.getElementById("carregando-texto");
        if (texto) texto.textContent = form.getAttribute("data-carregando") || "Processando…";
        if (painel) painel.hidden = false;
        form.querySelectorAll("button").forEach(function (botao) {{ botao.disabled = true; }});
      }});
    }});
    function abrirEan(evento) {{
      evento.preventDefault();
      var codigo = (evento.target.ean.value || "").replace(/\\D/g, "");
      if (codigo) location.href = "/v1/ean/" + codigo;
    }}
  </script>
</body>
</html>
"""


def _resultado(pacote: dict, token: str) -> str:
    resumo = pacote["resumo"]
    cartoes = [
        ("lidos", "Lidos"),
        ("definitivos", "Definitivos"),
        ("aplicados", "Aplicados"),
        ("ambiguos", "Ambíguos"),
        ("sem_par", "Sem par"),
        ("excluidos", "Excluídos"),
        ("invalidos", "Campo inválido"),
    ]
    blocos = "".join(
        f'<div class="contagem"><strong>{resumo[chave]}</strong><span>{html.escape(rotulo)}</span></div>'
        for chave, rotulo in cartoes
    )
    linhas = []
    for item in pacote["amostra"]:
        linhas.append(
            "<tr>"
            f"<td>{html.escape(item.item.descricao)}</td>"
            f"<td>{html.escape(item.descricao_sistema)}</td>"
            f"<td><code>{html.escape(item.ncm_anterior)}</code> → <code>{html.escape(item.ncm_novo)}</code></td>"
            f"<td><code>{html.escape(item.nfe_anterior)}</code> → <code>{html.escape(item.nfe_novo)}</code></td>"
            f"<td>{html.escape(item.tipo_casamento)}</td>"
            "</tr>"
        )
    if linhas:
        tabela = (
            "<h2>Amostra das alterações</h2>"
            '<div class="painel"><table><thead><tr>'
            "<th>Descrição na contabilidade</th><th>Descrição no sistema</th>"
            "<th>NCM</th><th>Classif. NFe e NFCe</th><th>Casamento</th>"
            "</tr></thead><tbody>"
            + "".join(linhas)
            + "</tbody></table></div>"
        )
        if pacote["total_aplicados"] > len(pacote["amostra"]):
            tabela += f'<p class="nota">Mostrando {len(pacote["amostra"])} de {pacote["total_aplicados"]} alterações. O relatório traz a lista completa.</p>'
    else:
        tabela = '<p class="nota">Nenhuma linha do CSV foi alterada. O relatório lista os produtos sem descrição correspondente, os ambíguos e os que ficaram de fora.</p>'
    return f"""
    <section>
      <div class="contagens">{blocos}</div>
      <div class="acoes">
        <a href="/baixar/csv?id={html.escape(token)}">Baixar CSV para importar</a>
        <a class="secundario" href="/baixar/relatorio?id={html.escape(token)}">Baixar relatório</a>
      </div>
      {_coleta(pacote.get("coleta"))}
      {tabela}
    </section>
    """


ABAS = (
    ("ajuste", "Ajuste"),
    ("importar", "Importar"),
    ("produtos", "Produtos"),
    ("consulta", "Consulta"),
    ("base", "Base"),
    ("configuracao", "Configurações"),
)


def _aba_ativa(pedida, pacote, resumo, busca: str) -> str:
    if pedida in {chave for chave, _rotulo in ABAS}:
        return pedida
    if pacote and pacote.get("tipo") == "sanitizacao":
        return "importar"
    if pacote:
        return "ajuste"
    if resumo and "inseridos" in resumo:
        return "importar"
    if resumo and resumo.get("configuracao"):
        return "configuracao"
    if resumo:
        return "base"
    if busca:
        return "produtos"
    return "ajuste"


def _abas(ativa: str, token: str | None, acao_token: str | None) -> str:
    links = []
    for chave, rotulo in ABAS:
        classe = ' class="ativa"' if chave == ativa else ""
        endereco = html.escape(_endereco_aba(chave, token, acao_token))
        links.append(f'<a href="{endereco}"{classe}>{rotulo}</a>')
    return f'<nav class="abas">{"".join(links)}</nav>'


def _endereco_aba(aba: str, token: str | None, acao_token: str | None) -> str:
    partes = [f"aba={aba}"]
    if token:
        partes.append(f"id={quote(token)}")
    if acao_token:
        partes.append(f"acao={quote(acao_token)}")
    return "/?" + "&".join(partes)


def _catalogo(lista: dict, aviso: str | None) -> str:
    busca = lista.get("busca") or ""
    aviso_html = f'<p class="erro">{html.escape(aviso)}</p>' if aviso else ""
    linhas = []
    for item in lista.get("itens") or []:
        linhas.append(
            "<tr>"
            f"<td>{html.escape(item.get('descricao') or '')}</td>"
            f"<td><code>{html.escape(item.get('ean') or '')}</code></td>"
            f"<td><code>{html.escape(item.get('ncm') or '')}</code></td>"
            f"<td><code>{html.escape(item.get('cclass_trib') or '')}</code></td>"
            f"<td>{html.escape(_situacao(item.get('situacao')))}</td>"
            f"<td><code>{html.escape(item.get('cfop_pdv') or '')}</code></td>"
            f"<td><code>{html.escape(item.get('cfop_nfe') or '')}</code></td>"
            "</tr>"
        )
    if linhas:
        tabela = (
            '<div class="painel"><table><thead><tr>'
            "<th>Descrição</th><th>EAN</th><th>NCM</th><th>cClassTrib</th>"
            "<th>Situação</th><th>CFOP PDV</th><th>CFOP NF-e</th>"
            "</tr></thead><tbody>"
            + "".join(linhas)
            + "</tbody></table></div>"
        )
    else:
        tabela = '<p class="nota">Nenhum produto encontrado na base.</p>'
    paginas = ""
    if lista.get("paginas", 1) > 1:
        anterior = _link_pagina(busca, lista["pagina"] - 1) if lista["pagina"] > 1 else ""
        seguinte = _link_pagina(busca, lista["pagina"] + 1) if lista["pagina"] < lista["paginas"] else ""
        paginas = (
            '<p class="paginas">'
            + (f'<a href="{html.escape(anterior)}">Anterior</a>' if anterior else "")
            + f'<span>Página {lista["pagina"]} de {lista["paginas"]}</span>'
            + (f'<a href="{html.escape(seguinte)}">Próxima</a>' if seguinte else "")
            + "</p>"
        )
    total = lista.get("total", 0)
    quantidade = "1 produto na base." if total == 1 else f"{total} produtos na base."
    return (
        f"{aviso_html}"
        f'<p class="nota">{quantidade}</p>'
        '<form class="busca" method="get" action="/">'
        '<input type="hidden" name="aba" value="produtos">'
        '<label>Buscar produto'
        "<span>Descrição, EAN, NCM ou cClassTrib.</span>"
        f'<input name="q" value="{html.escape(busca)}">'
        "</label>"
        '<button type="submit">Buscar</button></form>'
        + tabela
        + paginas
    )


def _link_pagina(busca: str, pagina: int) -> str:
    return f"/?aba=produtos&pagina={pagina}&q={quote(busca)}"


def _situacao(valor) -> str:
    return {"correto": "Correto", "corrigido": "Corrigido"}.get(valor or "", valor or "")


_FILTROS_CADASTRO = (
    ("sanitizado", "Sanitizados"),
    ("conforme", "Já conformes"),
    ("sem_par", "Sem cadastro"),
    ("conflito", "Conflitos"),
    ("pendente", "Pendentes"),
    ("todos", "Todos"),
)
_ROTULOS_CADASTRO = {
    "sanitizado": "Sanitizado",
    "conforme": "Já conforme",
    "sem_par": "Sem cadastro",
    "conflito": "Conflito",
    "pendente": "Pendente",
}


def _cadastros_vazios(situacao: str = "sanitizado") -> dict:
    escolha = situacao if situacao in SITUACOES_CADASTRO or situacao == "todos" else "sanitizado"
    return {
        "itens": [],
        "total": 0,
        "pagina": 1,
        "paginas": 1,
        "situacao": escolha,
        "contagens": {chave: 0 for chave in SITUACOES_CADASTRO},
        "arquivo": "",
        "lidos": 0,
        "quando": "",
    }


def _sanitizados(cadastros: dict, aviso: str | None, pacote: dict | None, token: str | None, acao_token: str | None) -> str:
    aviso_html = f'<p class="erro">{html.escape(aviso)}</p>' if aviso else ""
    contagens = cadastros.get("contagens") or {}
    chips = []
    for chave, rotulo in _FILTROS_CADASTRO:
        classe = ' class="ativa"' if cadastros.get("situacao") == chave else ""
        quantidade = sum(contagens.values()) if chave == "todos" else contagens.get(chave, 0)
        endereco = html.escape(_link_cadastro(chave, 1, token, acao_token))
        chips.append(f'<a href="{endereco}"{classe}>{html.escape(rotulo)} ({quantidade})</a>')
    arquivo = cadastros.get("arquivo") or ""
    if arquivo:
        quando = f" em {cadastros['quando']}" if cadastros.get("quando") else ""
        lidos = cadastros.get("lidos") or 0
        resumo = f'<p class="nota">Último CSV: {html.escape(arquivo)}{html.escape(quando)}. {lidos} produtos lidos.</p>'
    else:
        resumo = '<p class="nota">Nenhum CSV do sistema foi sanitizado ainda.</p>'
    baixar = ""
    if token and pacote and pacote.get("tipo") == "sanitizacao":
        baixar = (
            '<div class="acoes">'
            f'<a href="/baixar/csv?id={html.escape(token)}">Baixar CSV sanitizado</a>'
            "</div>"
        )
    linhas = []
    for item in cadastros.get("itens") or []:
        situacao = item.get("situacao") or ""
        classe = ' class="sanitizado"' if situacao == "sanitizado" else ""
        linhas.append(
            f"<tr{classe}>"
            f"<td>{html.escape(item.get('identificador') or '')}</td>"
            f"<td>{html.escape(item.get('descricao') or '')}</td>"
            f"<td><code>{html.escape(item.get('ean') or '')}</code></td>"
            f"<td>{_seta(item.get('ncm_original'), item.get('ncm'))}</td>"
            f"<td>{_seta(item.get('cest_original'), item.get('cest'))}</td>"
            f"<td>{_seta(item.get('cclass_original'), item.get('cclass'))}</td>"
            f"<td>{html.escape(_ROTULOS_CADASTRO.get(situacao, situacao))}</td>"
            "</tr>"
        )
    if linhas:
        tabela = (
            '<div class="painel"><table><thead><tr>'
            "<th>Código</th><th>Descrição</th><th>EAN</th><th>NCM</th><th>CEST</th>"
            "<th>cClassTrib</th><th>Situação</th>"
            "</tr></thead><tbody>"
            + "".join(linhas)
            + "</tbody></table></div>"
        )
    elif arquivo:
        tabela = '<p class="nota">Nenhum cadastro neste filtro.</p>'
    else:
        tabela = '<p class="nota">Envie o CSV do sistema para ver os cadastros sanitizados.</p>'
    paginas = ""
    if cadastros.get("paginas", 1) > 1:
        situacao = cadastros.get("situacao") or "sanitizado"
        anterior = _link_cadastro(situacao, cadastros["pagina"] - 1, token, acao_token) if cadastros["pagina"] > 1 else ""
        seguinte = (
            _link_cadastro(situacao, cadastros["pagina"] + 1, token, acao_token)
            if cadastros["pagina"] < cadastros["paginas"]
            else ""
        )
        paginas = (
            '<p class="paginas">'
            + (f'<a href="{html.escape(anterior)}">Anterior</a>' if anterior else "")
            + f'<span>Página {cadastros["pagina"]} de {cadastros["paginas"]}</span>'
            + (f'<a href="{html.escape(seguinte)}">Próxima</a>' if seguinte else "")
            + "</p>"
        )
    return (
        "<h2>Cadastros sanitizados</h2>"
        f"{aviso_html}{resumo}{baixar}"
        f'<nav class="filtros">{"".join(chips)}</nav>'
        + tabela
        + paginas
    )


def _seta(antes, depois) -> str:
    origem = antes or ""
    destino = depois or ""
    if origem == destino:
        return f"<code>{html.escape(origem)}</code>" if origem else ""
    return f"<code>{html.escape(origem)}</code> → <code>{html.escape(destino)}</code>"


def _link_cadastro(situacao: str, pagina: int, token: str | None, acao_token: str | None) -> str:
    partes = ["aba=importar", f"situacao={quote(situacao)}", f"pagina={pagina}"]
    if token:
        partes.append(f"id={quote(token)}")
    if acao_token:
        partes.append(f"acao={quote(acao_token)}")
    return "/?" + "&".join(partes)


def _configuracoes(chave: dict, resumo: dict | None) -> str:
    if chave.get("indisponivel"):
        estado = '<p class="erro">A base fiscal está indisponível.</p>'
    elif chave.get("origem") == "ambiente":
        estado = (
            '<p class="nota">A chave em uso veio do ambiente do servidor, '
            f'terminada em {html.escape(chave.get("sufixo") or "")}. '
            "Ela tem prioridade sobre a chave salva aqui.</p>"
        )
    elif chave.get("origem") == "base":
        estado = (
            '<p class="nota">Há uma chave salva nesta base, '
            f'terminada em {html.escape(chave.get("sufixo") or "")}.</p>'
        )
    else:
        estado = (
            '<p class="nota">Nenhuma chave configurada. A conferência atualiza o texto legal, '
            "mas não pede sugestão ao Gemini.</p>"
        )
    aviso = ""
    if resumo and resumo.get("configuracao") == "salva":
        aviso = '<p class="nota">Chave salva. Ela não aparece de novo nesta tela.</p>'
    elif resumo and resumo.get("configuracao") == "removida":
        aviso = '<p class="nota">Chave removida da base.</p>'
    return (
        "<section>"
        "<h2>Configurações</h2>"
        f"{aviso}{estado}"
        '<form method="post" action="/configuracao" data-carregando="Salvando a configuração…">'
        "<label>Chave da API do Gemini"
        "<span>Crie em aistudio.google.com/apikey. A chave fica só na base local e não entra no Git.</span>"
        '<input type="password" name="gemini_api_key" autocomplete="off" placeholder="Cole a chave aqui">'
        "</label>"
        '<button type="submit">Salvar chave</button>'
        "</form>"
        '<form class="linha" method="post" action="/configuracao" data-carregando="Removendo a chave salva…">'
        '<input type="hidden" name="remover" value="1">'
        '<button class="secundario" type="submit">Remover chave salva</button>'
        "</form>"
        "</section>"
    )


def _contexto_base() -> dict:
    sugestoes, aviso_base = _pendencias()
    return {"sugestoes": sugestoes, "aviso_base": aviso_base}


def _pendencias() -> tuple[list, str | None]:
    try:
        return listar_sugestoes_pendentes(), None
    except Exception:
        return [], "A base fiscal está indisponível."


def _base(sugestoes: list, aviso_base: str | None, resumo: dict | None) -> str:
    aviso = f'<p class="erro">{html.escape(aviso_base)}</p>' if aviso_base else ""
    blocos = ""
    if resumo:
        rotulos = [
            ("ncms", "NCMs oficiais"),
            ("cclasses", "cClassTrib"),
            ("inseridos", "Novos na base"),
            ("atualizados", "Cadastros atualizados"),
            ("conflitos", "Conflitos"),
            ("ignorados", "Fora da base"),
            ("sugestoes", "Sugestões"),
            ("sem_chave", "Sem chave do Gemini"),
            ("rejeitadas", "Sugestões rejeitadas"),
        ]
        partes = [
            f'<div class="contagem"><strong>{resumo[chave]}</strong><span>{html.escape(rotulo)}</span></div>'
            for chave, rotulo in rotulos
            if chave in resumo
        ]
        if resumo.get("erro"):
            partes.append(f'<p class="erro">{html.escape(str(resumo["erro"]))}</p>')
        if resumo.get("decisao"):
            texto = "Sugestão aceita e publicada." if resumo["decisao"] == "aceita" else "Sugestão recusada."
            partes.append(f'<p class="nota">{texto}</p>')
        if resumo.get("token"):
            partes.append(
                '<p class="nota">Token de '
                f'{html.escape(resumo.get("nome") or "parceiro")}: '
                f'<code>{html.escape(resumo["token"])}</code>. '
                "Guarde agora; ele não será mostrado de novo. "
                "Envie em Authorization: Bearer no POST /v1/saneamento.</p>"
            )
        blocos = f'<div class="contagens">{"".join(partes)}</div>'
    linhas = []
    for item in sugestoes:
        analise = item.get("analise") or ""
        duvida = "Sim" if item.get("duvida") else "Não"
        linhas.append(
            "<tr>"
            f"<td>{html.escape(item.get('descricao') or '')}</td>"
            f"<td><code>{html.escape(item.get('ncm_atual') or '')}</code></td>"
            f"<td><code>{html.escape(item.get('ncm_sugerido') or '')}</code></td>"
            f"<td><code>{html.escape(item.get('cclass_sugerido') or '')}</code></td>"
            f"<td>{html.escape(duvida)}</td>"
            f"<td>{html.escape(analise)}</td>"
            "<td>"
            f'<form class="linha" method="post" action="/sugestoes/aceitar" data-carregando="Atualizando a sugestão…">'
            f'<input type="hidden" name="id" value="{int(item["id"])}">'
            '<button type="submit">Aceitar</button></form> '
            f'<form class="linha" method="post" action="/sugestoes/recusar" data-carregando="Atualizando a sugestão…">'
            f'<input type="hidden" name="id" value="{int(item["id"])}">'
            '<button class="secundario" type="submit">Recusar</button></form>'
            "</td></tr>"
        )
    tabela = ""
    if linhas:
        tabela = (
            "<h2>Sugestões pendentes</h2>"
            '<div class="painel"><table><thead><tr>'
            "<th>Descrição</th><th>NCM atual</th><th>NCM sugerido</th><th>cClassTrib</th>"
            "<th>Dúvida</th><th>Embasamento</th><th></th>"
            "</tr></thead><tbody>"
            + "".join(linhas)
            + "</tbody></table></div>"
        )
    return (
        "<section>"
        "<h2>Base oficial e classificação</h2>"
        '<p class="nota">Atualiza NCM, cClassTrib e o embasamento legal a partir do Siscomex e da Calculadora da Receita. '
        "A conferência só pede uma sugestão ao Gemini quando o NCM deixou de existir ou a descrição do EAN mudou, "
        "e envia esses produtos no mesmo pedido para a tabela oficial não se repetir. "
        "O cadastro publicado só muda ao aceitar a sugestão.</p>"
        f"{aviso}{blocos}"
        '<div class="acoes">'
        '<form class="linha" method="post" action="/tabelas/atualizar" data-carregando="Atualizando as tabelas oficiais…"><button type="submit">Atualizar tabelas oficiais</button></form>'
        '<form class="linha" method="post" action="/cadastros/conferir" data-carregando="Conferindo os cadastros…"><button type="submit">Conferir cadastros</button></form>'
        "</div>"
        '<form method="post" action="/parceiros" data-carregando="Emitindo o token do parceiro…">'
        '<div class="campos">'
        "<label>Parceiro ERP"
        "<span>Emite um token para o POST /v1/saneamento. O token aparece uma vez.</span>"
        '<input name="nome" placeholder="Nome do ERP" required>'
        "</label>"
        "<label>CNPJ do parceiro"
        "<span>14 dígitos, com ou sem pontuação.</span>"
        '<input name="cnpj" inputmode="numeric" placeholder="00000000000000" required>'
        "</label>"
        "</div>"
        '<button type="submit">Emitir token</button>'
        "</form>"
        f"{tabela}</section>"
    )


def _coleta(coleta: dict | None) -> str:
    if not coleta:
        return ""
    if coleta.get("erro"):
        return f'<p class="erro">A base fiscal não recebeu esta coleta: {html.escape(coleta["erro"])}</p>'
    cartoes = [
        ("inseridos", "Novos na base"),
        ("atualizados", "Já na base"),
        ("conflitos", "Conflitos"),
    ]
    blocos = "".join(
        f'<div class="contagem"><strong>{coleta[chave]}</strong><span>{html.escape(rotulo)}</span></div>'
        for chave, rotulo in cartoes
    )
    return f'<h2>Base fiscal</h2><div class="contagens">{blocos}</div>'


if __name__ == "__main__":
    main()
