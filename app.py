"""Micro App: detecta palabras de search terms que gastan sin convertir."""

import hashlib
import json
import os
import tempfile

import duckdb
import pandas as pd
import streamlit as st
from openai import OpenAI, OpenAIError

REQUIRED_COLUMNS = {"Campaign", "search_term", "cost", "conversions"}
MIN_COST = 5.0
MIN_WORD_LENGTH = 3

STOPWORDS_ES = frozenset({
    "con", "para", "por", "las", "los", "del", "que", "una", "uno", "unos",
    "unas", "sin", "sobre", "entre", "como", "mas", "más", "muy", "este",
    "esta", "esto", "ese", "esa", "son", "hay", "cual", "donde", "cuando",
})

# Patrones de basura B2B; se evalúan sobre la palabra completa (sin grupos de captura).
JUNK_PATTERNS_ES = (
    r"gratis", r"gratuit[oa]s?", r"empleos?", r"trabajos?", r"barat[oa]s?",
    r"usad[oa]s?", r"cursos?", r"pdf", r"descargar?", r"tutorial(?:es)?",
)
JUNK_PATTERNS_EN = (
    # Empleos / búsqueda de trabajo
    r"jobs?", r"careers?", r"hiring", r"salar(?:y|ies)", r"pay(?:ing)?", r"internships?",
    # Descargas / contenido gratuito
    r"free(?:bies?)?", r"download(?:s|ing|able)?", r"pdfs", r"templates?",
    r"tutorials?", r"courses?",
    # Intención irrelevante: clientes existentes, quejas, research de precio bajo.
    # "sign in" y "near me" se parten en palabras: se detectan por su token distintivo.
    r"log-?ins?", r"sign-?in", r"support", r"tracking", r"refund(?:s|ed|ing)?",
    r"scam(?:s|mers?)?", r"reviews?", r"near", r"cheap(?:er|est)?", r"used",
)
JUNK_PATTERNS = JUNK_PATTERNS_ES + JUNK_PATTERNS_EN
JUNK_REGEX = rf"^(?:{'|'.join(JUNK_PATTERNS)})$"
LABEL_NEGATIVE = "Negativa Automática"
LABEL_REVIEW = "A Revisar"

OPENAI_MODEL = "gpt-4o-mini"
AI_LABELS = frozenset({"Basura", "Relevante"})
AI_STATE_KEY = "ai_result"
PROMPT_TEMPLATE = (
    "Sos un experto en Google Ads. El cliente vende: {contexto}. "
    "Evaluá esta lista de palabras de búsqueda que gastaron dinero sin generar ventas. "
    "Clasificá cada una indicando si es 'Relevante' (tiene intención de compra alineada "
    "al negocio) o 'Basura' (fuera de contexto o mala calidad). "
    'Respondé ESTRICTAMENTE con este JSON: {{"resultados": [{{"palabra": "...", '
    '"clasificacion": "Basura o Relevante", "motivo": "breve justificación"}}]}}. '
    "Palabras a analizar: {lista_palabras}"
)

# El costo completo del término se atribuye a cada palabra que lo compone.
WORDS_QUERY = """
WITH words AS (
    SELECT
        lower(trim(unnest(string_split(search_term, ' ')))) AS word,
        cost,
        conversions
    FROM terms
)
SELECT
    word,
    round(sum(cost), 2)      AS total_cost,
    sum(conversions)         AS total_conversions,
    count(*)                 AS term_count
FROM words
WHERE word <> ''
GROUP BY word
HAVING sum(conversions) = 0 AND sum(cost) > ?
ORDER BY total_cost DESC
"""


def analyze_terms(file_bytes: bytes, min_cost: float = MIN_COST) -> pd.DataFrame:
    """Lee el CSV con DuckDB y devuelve palabras con 0 conversiones y costo > min_cost."""
    # DuckDB lee file-like solo con fsspec; un archivo temporal evita esa dependencia.
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(file_bytes)
    con = duckdb.connect()
    try:
        terms = con.read_csv(tmp.name, header=True)
        missing = REQUIRED_COLUMNS - set(terms.columns)
        if missing:
            raise ValueError(f"Faltan columnas en el CSV: {sorted(missing)}")
        con.register("terms", terms)
        return con.execute(WORDS_QUERY, [min_cost]).df()
    finally:
        con.close()
        os.unlink(tmp.name)


def filtrar_stopwords(df: pd.DataFrame) -> pd.DataFrame:
    """Excluye palabras cortas y stopwords en español (devuelve un DataFrame nuevo)."""
    keep = (df["word"].str.len() >= MIN_WORD_LENGTH) & ~df["word"].isin(STOPWORDS_ES)
    return df.loc[keep].reset_index(drop=True)


def aplicar_regex(df: pd.DataFrame) -> pd.DataFrame:
    """Agrega 'Clasificacion' según si la palabra matchea algún patrón de basura."""
    is_junk = df["word"].str.contains(JUNK_REGEX, case=False, regex=True, na=False)
    return df.assign(Clasificacion=is_junk.map({True: LABEL_NEGATIVE, False: LABEL_REVIEW}))


def _validar_resultados(payload: object, esperadas: set[str]) -> dict[str, tuple[str, str]]:
    """Descarta items malformados, palabras no pedidas y etiquetas fuera de AI_LABELS."""
    items = payload.get("resultados", []) if isinstance(payload, dict) else []
    validos: dict[str, tuple[str, str]] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        palabra = str(item.get("palabra", "")).strip().lower()
        clasificacion = str(item.get("clasificacion", "")).strip()
        if palabra in esperadas and clasificacion in AI_LABELS:
            validos[palabra] = (clasificacion, str(item.get("motivo", "")).strip())
    return validos


def _consultar_openai(palabras: list[str], contexto: str, api_key: str) -> dict[str, tuple[str, str]]:
    prompt = PROMPT_TEMPLATE.format(contexto=contexto, lista_palabras=", ".join(palabras))
    client = OpenAI(api_key=api_key, timeout=60)
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    payload = json.loads(response.choices[0].message.content or "{}")
    return _validar_resultados(payload, set(palabras))


def analizar_con_ia(df: pd.DataFrame, contexto: str, api_key: str) -> pd.DataFrame:
    """Reclasifica con OpenAI las palabras 'A Revisar' (devuelve un DataFrame nuevo)."""
    if not api_key or not contexto.strip():
        st.warning("Ingresá tu OpenAI API Key en la barra lateral y el contexto de tu negocio.")
        return df
    dudosas = df.loc[df["Clasificacion"] == LABEL_REVIEW, "word"].tolist()
    if not dudosas:
        st.info("No hay palabras 'A Revisar' para analizar.")
        return df

    resultados = _consultar_openai(dudosas, contexto.strip(), api_key)
    sin_respuesta = len(dudosas) - len(resultados)
    if sin_respuesta:
        st.warning(f"La IA no devolvió una clasificación válida para {sin_respuesta} palabra(s); siguen 'A Revisar'.")

    ia = df["word"].map(resultados)
    return df.assign(
        Clasificacion=ia.str[0].fillna(df["Clasificacion"]),
        **{"Motivo IA": ia.str[1].fillna("")},
    )


def _render_results(result: pd.DataFrame) -> None:
    negatives = int((result["Clasificacion"] == LABEL_NEGATIVE).sum())
    col_total, col_neg, col_cost = st.columns(3)
    col_total.metric("Total Palabras Encontradas", len(result))
    col_neg.metric("Negativas Automáticas Detectadas", negatives)
    col_cost.metric("Gasto desperdiciado (suma por palabra)", f"${result['total_cost'].sum():,.2f}")
    st.dataframe(result, use_container_width=True, hide_index=True)
    st.download_button(
        "Descargar CSV limpio",
        data=result.to_csv(index=False).encode("utf-8-sig"),
        file_name="palabras_clasificadas.csv",
        mime="text/csv",
    )


def main() -> None:
    st.set_page_config(page_title="Negative Keyword Finder", page_icon="🔎", layout="wide")
    st.title("🔎 Negative Keyword Finder")
    st.caption(f"Palabras con 0 conversiones y costo mayor a ${MIN_COST:.0f}")
    api_key = st.sidebar.text_input("OpenAI API Key", type="password") or os.environ.get("OPENAI_API_KEY", "")

    uploaded = st.file_uploader("Subí el CSV de términos de búsqueda", type="csv")
    if uploaded is None:
        st.info("Columnas esperadas: " + ", ".join(sorted(REQUIRED_COLUMNS)))
        return

    try:
        result = aplicar_regex(filtrar_stopwords(analyze_terms(uploaded.getvalue())))
    except (ValueError, duckdb.Error) as exc:
        st.error(f"No se pudo procesar el archivo: {exc}")
        return

    if result.empty:
        st.success("No hay palabras que cumplan el filtro.")
        return

    # El resultado de la IA se guarda en session_state para sobrevivir los reruns (ej: download).
    file_key = hashlib.sha256(uploaded.getvalue()).hexdigest()
    contexto = st.text_input("Contexto de tu negocio (¿Qué vendés o qué servicio ofrecés?)")
    if st.button("Analizar Dudosos con IA"):
        try:
            with st.spinner("Consultando a OpenAI..."):
                st.session_state[AI_STATE_KEY] = (file_key, analizar_con_ia(result, contexto, api_key))
        except (OpenAIError, json.JSONDecodeError) as exc:
            st.error(f"Falló el análisis con IA ({type(exc).__name__}). Revisá la API Key y reintentá.")

    cached = st.session_state.get(AI_STATE_KEY)
    _render_results(cached[1] if cached and cached[0] == file_key else result)


if __name__ == "__main__":
    main()
