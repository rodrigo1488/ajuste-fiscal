import re
import unicodedata

EMBALAGEM = {"SH", "C", "KG", "ML"}


def normalizar_texto(valor) -> str:
    if valor is None:
        return ""
    texto = str(valor).strip()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(caractere for caractere in texto if not unicodedata.combining(caractere))
    texto = texto.upper()
    texto = re.sub(r"[^A-Z0-9]+", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def tokenizar(texto_norm: str) -> list[str]:
    if not texto_norm:
        return []
    return texto_norm.split()


def token_embalagem(token: str) -> bool:
    return token in EMBALAGEM or token.isdigit()


def codigo_fiscal(valor, tamanho: int) -> str | None:
    """Extrai um código numérico com zero à esquerda até o tamanho esperado.

    Aceita número do Excel, texto com pontos (3402.90.90) e texto já numérico.
    Devolve None quando o valor está vazio ou tem dígitos demais.
    """
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, float):
        if not valor.is_integer():
            return None
        bruto = str(int(valor))
    elif isinstance(valor, int):
        bruto = str(valor)
    else:
        bruto = str(valor).strip()
        if re.fullmatch(r"\d+\.0", bruto):
            bruto = bruto[:-2]
        else:
            bruto = re.sub(r"[\s.\-]", "", bruto)
            if not bruto.isdigit():
                return None
    if not bruto.isdigit() or len(bruto) > tamanho or len(bruto) == 0:
        return None
    return bruto.zfill(tamanho)


def codigo_ncm(valor) -> str | None:
    return codigo_fiscal(valor, 8)


def codigo_cclass(valor) -> str | None:
    return codigo_fiscal(valor, 6)


def codigo_cest(valor) -> str | None:
    return codigo_fiscal(valor, 7)


def codigo_cfop(valor) -> str | None:
    return codigo_fiscal(valor, 4)


def normalizar_ean(valor) -> str | None:
    digitos = "".join(caractere for caractere in _bruto_numerico(valor) if caractere.isdigit())
    if len(digitos) in {8, 12, 13, 14}:
        return digitos
    return None


def numero_fiscal(valor) -> str | None:
    bruto = _bruto_numerico(valor).replace(",", ".")
    if not bruto:
        return None
    if bruto.endswith("%"):
        bruto = bruto[:-1]
    if not re.fullmatch(r"\d+(?:\.\d+)?", bruto):
        return None
    return bruto


def partir_natureza(valor) -> tuple[str | None, str | None]:
    if valor is None or isinstance(valor, bool):
        return None, None
    texto = str(valor).strip()
    if isinstance(valor, float) and valor.is_integer():
        texto = str(int(valor))
    if not texto:
        return None, None
    encontrado = re.match(r"^(\d+)\s*[-–—:]\s*(.+)$", texto)
    if encontrado:
        return encontrado.group(1), encontrado.group(2).strip()
    if texto.isdigit():
        return texto, None
    return None, texto


def _bruto_numerico(valor) -> str:
    if valor is None or isinstance(valor, bool):
        return ""
    if isinstance(valor, float):
        if valor.is_integer():
            return str(int(valor))
        return str(valor)
    if isinstance(valor, int):
        return str(valor)
    return str(valor).strip()


def tipo_status(status) -> str:
    texto = normalizar_texto(status)
    if texto in {"CORRETO", "INCORRETO"}:
        return "definitivo"
    return "excluido"


def casa_por_embalagem(descricao_a: str, descricao_b: str) -> bool:
    """True quando uma descrição contém a outra e o que sobra é só embalagem.

    Embalagem aceita: SH, C/12 (tokens C e número), KG, ML e números.
    Descrições iguais ficam para o casamento exato.
    """
    tokens_a = tokenizar(descricao_a)
    tokens_b = tokenizar(descricao_b)
    if not tokens_a or not tokens_b or tokens_a == tokens_b:
        return False
    if len(tokens_a) < len(tokens_b):
        curto, longo = tokens_a, tokens_b
    else:
        curto, longo = tokens_b, tokens_a
    if not any(not token_embalagem(token) for token in curto):
        return False
    limite = len(longo) - len(curto) + 1
    for inicio in range(limite):
        if longo[inicio : inicio + len(curto)] != curto:
            continue
        sobra = longo[:inicio] + longo[inicio + len(curto) :]
        if sobra and all(token_embalagem(token) for token in sobra):
            return True
    return False
