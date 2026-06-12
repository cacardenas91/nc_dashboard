"""Dashboard NC Workflow - Streamlit.

Vista interactiva sobre el historial de solicitudes de notas credito.
Lee directamente del state.sqlite (no toca Outlook ni SAP).

Para ejecutar:
    streamlit run dashboard.py

Para que cartera/comerciales accedan desde la red:
    streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501
    luego comparte:  http://<tu-IP-en-la-red>:8501
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

# Los imports del ORM (sqlalchemy, src.config, src.state_store) se hacen de forma
# PEREZOSA dentro de las funciones de carga. Asi el MISMO dashboard funciona:
#   - en local: leyendo state.sqlite via el ORM
#   - en Streamlit Community Cloud: leyendo un snapshot parquet (sin ORM ni BD)
SNAPSHOT_REQUESTS = "data/snapshot_requests.parquet"
SNAPSHOT_LINES = "data/snapshot_lines.parquet"

# ---------------------------------------------------------------------------
# Config y estilo
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="NC Workflow - Dashboard",
    layout="wide",
    page_icon="📊",
    initial_sidebar_state="expanded",
)

STATUS_COLORS = {
    "INGESTED": "#9e9e9e",
    "CONSOLIDATED": "#1976d2",
    "PENDING_APPROVAL": "#f57c00",
    "APPROVED": "#388e3c",
    "REJECTED": "#c62828",
    "SAP_RECORDING": "#7b1fa2",
    "SAP_RECORDED": "#00796b",
    "NOTIFIED": "#1565c0",
    "FAILED": "#d32f2f",
    "EXPIRED": "#616161",
}

SECTOR_COLORS = {
    "BNA": "#1976d2",
    "ALIMENTOS": "#388e3c",
    "CCC": "#f57c00",
    "DESCONOCIDO": "#9e9e9e",
}


# ---------------------------------------------------------------------------
# Carga de datos (cacheada)
# ---------------------------------------------------------------------------
@st.cache_resource
def get_store():
    from src.config import load_config
    from src.state_store import StateStore
    cfg = load_config("config.yaml")
    return StateStore(cfg.paths["state_db"])


# ---- Carga de SOLICITUDES: intenta ORM (local); si falla, usa snapshot (nube) --
@st.cache_data(ttl=60)
def load_requests_df() -> pd.DataFrame:
    try:
        return _load_requests_orm()
    except Exception:
        return _load_requests_snapshot()


def _load_requests_orm() -> pd.DataFrame:
    """Trae todas las solicitudes a un DataFrame plano desde el ORM/BD local."""
    from sqlalchemy import select
    from src.state_store import Request

    store = get_store()
    rows = []
    with store.session() as s:
        for r in s.scalars(select(Request)):
            sap_docs = []
            if r.sap_documents:
                try:
                    sap_docs = json.loads(r.sap_documents)
                except Exception:
                    sap_docs = [r.sap_documents]
            tiempo_aprob_h = None
            if r.received_at and r.approved_at:
                delta = r.approved_at - r.received_at
                tiempo_aprob_h = delta.total_seconds() / 3600.0
            rows.append({
                "id": r.id,
                "status": r.status.value if hasattr(r.status, "value") else str(r.status),
                "ejecutivo": r.sender_name or "",
                "ejecutivo_email": r.sender_email or "",
                "cliente_nit": r.cliente_nit or "",
                "cliente_nombre": r.cliente_nombre or "",
                "sector": r.dominant_sector or "DESCONOCIDO",
                "tipologia": r.tipologia or "",
                "tipo_nota": r.tipo_nota or "",
                "palanca": r.palanca_comercial or "",
                "texto_cuenta_puente": getattr(r, "texto_cuenta_puente", "") or "",
                "monto": float(r.total_amount or 0),
                "recibido": r.received_at,
                "actualizado": r.updated_at,
                "aprobado": r.approved_at,
                "aprobador": r.approver_email or "",
                "batch_id": r.approval_batch_id or "",
                "sap_docs": ", ".join(sap_docs),
                "n_sap_docs": len(sap_docs),
                "error": r.error_msg or "",
                "tiempo_aprob_horas": tiempo_aprob_h,
                "mes": r.received_at.strftime("%Y-%m") if r.received_at else None,
            })
    return pd.DataFrame(rows)


def _load_requests_snapshot() -> pd.DataFrame:
    """Lee el snapshot parquet (modo nube). Vacio si no existe."""
    import os
    if not os.path.exists(SNAPSHOT_REQUESTS):
        return pd.DataFrame()
    df = pd.read_parquet(SNAPSHOT_REQUESTS)
    for col in ("recibido", "actualizado", "aprobado"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


# ---- Carga de LINEAS de una solicitud: ORM (local) o snapshot (nube) -----------
@st.cache_data(ttl=60)
def load_lines_df(request_id: str) -> pd.DataFrame:
    try:
        return _load_lines_orm(request_id)
    except Exception:
        return _load_lines_snapshot(request_id)


def _load_lines_orm(request_id: str) -> pd.DataFrame:
    """Trae las lineas de detalle de una solicitud desde el ORM/BD local."""
    from sqlalchemy import select
    from src.state_store import LineItem

    store = get_store()
    rows = []
    with store.session() as s:
        for li in s.scalars(select(LineItem).where(LineItem.request_id == request_id)):
            rows.append({
                "material": li.material,
                "centro": li.sociedad,
                "deudor": li.deudor,
                "cantidad": li.cantidad,
                "descripcion": li.texto_descuento,
                "texto_cuenta_puente": getattr(li, "texto_cuenta_puente", "") or "",
                "sector": li.sector,
                "valor_sin_iva": float(li.valor_sin_iva or 0),
                "consecutivo": li.consecutivo,
            })
    return pd.DataFrame(rows)


def _load_lines_snapshot(request_id: str) -> pd.DataFrame:
    """Filtra las lineas del snapshot parquet (modo nube)."""
    import os
    if not os.path.exists(SNAPSHOT_LINES):
        return pd.DataFrame()
    df = pd.read_parquet(SNAPSHOT_LINES)
    out = df[df["request_id"] == request_id].drop(columns=["request_id"], errors="ignore")
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Filtro de fecha interactivo (periodo rapido / meses / rango personalizado)
# ---------------------------------------------------------------------------
def _rango_preset(opcion: str, hoy, min_date, max_date):
    if opcion == "Hoy":
        return hoy, hoy
    if opcion == "Últimos 7 días":
        return hoy - timedelta(days=6), hoy
    if opcion == "Últimos 30 días":
        return hoy - timedelta(days=29), hoy
    if opcion == "Últimos 90 días":
        return hoy - timedelta(days=89), hoy
    if opcion == "Este mes":
        return hoy.replace(day=1), hoy
    if opcion == "Mes anterior":
        primero_este = hoy.replace(day=1)
        fin_anterior = primero_este - timedelta(days=1)
        return fin_anterior.replace(day=1), fin_anterior
    return min_date, max_date  # "Todo"


def apply_date_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Filtro de fecha con tres modos: periodo rápido, por meses o rango."""
    recibido = df["recibido"].dropna()
    if recibido.empty:
        return df

    min_date = recibido.min().date()
    max_date = recibido.max().date()
    hoy = datetime.now().date()

    modo = st.sidebar.radio(
        "Filtrar fecha por",
        ["Periodo rápido", "Meses", "Rango personalizado"],
        index=0,
    )

    if modo == "Meses":
        meses_disp = sorted(df["mes"].dropna().unique(), reverse=True)
        sel_meses = st.sidebar.multiselect(
            "Meses (AAAA-MM)", meses_disp,
            default=meses_disp[:1] if meses_disp else [],
        )
        return df[df["mes"].isin(sel_meses)] if sel_meses else df

    if modo == "Rango personalizado":
        default_start = max(min_date, (datetime.now() - timedelta(days=90)).date())
        date_range = st.sidebar.date_input(
            "Rango de fechas (recibido)",
            value=(default_start, max_date),
            min_value=min_date, max_value=max_date,
        )
        if isinstance(date_range, tuple) and len(date_range) == 2:
            start, end = date_range
        else:
            start, end = min_date, max_date
    else:  # Periodo rápido
        opcion = st.sidebar.selectbox(
            "Periodo",
            ["Este mes", "Mes anterior", "Hoy", "Últimos 7 días",
             "Últimos 30 días", "Últimos 90 días", "Todo"],
            index=0,
        )
        start, end = _rango_preset(opcion, hoy, min_date, max_date)

    return df[(df["recibido"].dt.date >= start) & (df["recibido"].dt.date <= end)]


# ---------------------------------------------------------------------------
# Sidebar de filtros
# ---------------------------------------------------------------------------
def render_filters(df: pd.DataFrame) -> pd.DataFrame:
    st.sidebar.title("📊 NC Workflow")
    st.sidebar.markdown("**Filtros**")

    # Filtro de fecha interactivo (periodo rápido / meses / rango)
    df = apply_date_filter(df)

    # Sector
    sectores = sorted(df["sector"].dropna().unique())
    sel_sectores = st.sidebar.multiselect("Sector", sectores, default=sectores)
    if sel_sectores:
        df = df[df["sector"].isin(sel_sectores)]

    # Estado
    estados = sorted(df["status"].dropna().unique())
    sel_estados = st.sidebar.multiselect("Estado", estados, default=estados)
    if sel_estados:
        df = df[df["status"].isin(sel_estados)]

    # Ejecutivo
    ejecutivos = sorted(df["ejecutivo"].dropna().unique())
    sel_ej = st.sidebar.multiselect("Ejecutivo", ejecutivos, default=[])
    if sel_ej:
        df = df[df["ejecutivo"].isin(sel_ej)]

    # Cliente
    cliente_q = st.sidebar.text_input("Cliente (nombre o NIT contiene)", "")
    if cliente_q:
        q = cliente_q.upper()
        df = df[
            df["cliente_nombre"].str.upper().str.contains(q, na=False)
            | df["cliente_nit"].astype(str).str.contains(q, na=False)
        ]

    # Refresh manual
    st.sidebar.markdown("---")
    if st.sidebar.button("🔄 Refrescar datos"):
        st.cache_data.clear()
        st.rerun()

    st.sidebar.caption(
        f"Mostrando **{len(df)}** solicitud(es). Refresh automatico cada 60s."
    )

    return df


# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------
def render_kpis(df: pd.DataFrame) -> None:
    c1, c2, c3, c4, c5 = st.columns(5)
    total_sol = len(df)
    total_monto = df["monto"].sum()
    aprobadas = df[df["status"].isin(["APPROVED", "SAP_RECORDED", "NOTIFIED"])]
    pct_aprob = (len(aprobadas) / total_sol * 100) if total_sol else 0
    tiempo_prom = df["tiempo_aprob_horas"].dropna()
    tiempo_prom_h = tiempo_prom.mean() if len(tiempo_prom) else 0
    rechazadas = df[df["status"].isin(["REJECTED", "FAILED", "EXPIRED"])]
    pct_rech = (len(rechazadas) / total_sol * 100) if total_sol else 0

    c1.metric("Total solicitudes", f"{total_sol:,}")
    c2.metric("Monto total", f"${total_monto:,.0f}")
    c3.metric("Aprobadas / Procesadas", f"{pct_aprob:.1f}%",
              f"{len(aprobadas)} de {total_sol}")
    c4.metric("Tiempo prom. aprobacion",
              f"{tiempo_prom_h:.1f} h" if tiempo_prom_h else "—")
    c5.metric("Rechazadas / Falladas", f"{pct_rech:.1f}%",
              f"{len(rechazadas)} de {total_sol}", delta_color="inverse")


# ---------------------------------------------------------------------------
# Vista 1: Por sector y estado
# ---------------------------------------------------------------------------
def render_sector_estado(df: pd.DataFrame) -> None:
    st.subheader("📦 Por sector y estado")
    if df.empty:
        st.info("Sin datos para mostrar.")
        return

    col1, col2 = st.columns([1, 1])

    with col1:
        st.markdown("**Cantidad de solicitudes**")
        pivot_n = (
            df.groupby(["sector", "status"])
              .size()
              .reset_index(name="cantidad")
        )
        fig = px.bar(
            pivot_n, x="sector", y="cantidad", color="status",
            barmode="stack", text="cantidad",
            color_discrete_map=STATUS_COLORS,
            height=350,
        )
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0))
        fig.update_traces(textposition="inside")
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        st.markdown("**Monto $ acumulado**")
        pivot_m = (
            df.groupby(["sector", "status"])["monto"]
              .sum()
              .reset_index()
        )
        fig = px.bar(
            pivot_m, x="sector", y="monto", color="status",
            barmode="stack",
            color_discrete_map=STATUS_COLORS,
            height=350,
            labels={"monto": "Monto ($)"},
        )
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0),
                          yaxis_tickformat=",.0f")
        st.plotly_chart(fig, use_container_width=True)

    # Tabla cruzada
    st.markdown("**Resumen sector × estado**")
    tabla = df.pivot_table(
        index="sector", columns="status",
        values="monto", aggfunc="sum", fill_value=0,
    )
    tabla["TOTAL"] = tabla.sum(axis=1)
    tabla.loc["TOTAL"] = tabla.sum(axis=0)
    st.dataframe(
        tabla.style.format("${:,.0f}").background_gradient(
            cmap="Blues", subset=tabla.columns[:-1]
        ),
        use_container_width=True,
    )


# ---------------------------------------------------------------------------
# Vista 2: Tendencia mensual
# ---------------------------------------------------------------------------
def render_tendencia(df: pd.DataFrame) -> None:
    st.subheader("📈 Tendencia mensual")
    if df.empty or df["mes"].isna().all():
        st.info("Sin datos con fechas para mostrar tendencia.")
        return

    mensual = (
        df.dropna(subset=["mes"])
          .groupby(["mes", "sector"])
          .agg(cantidad=("id", "count"), monto=("monto", "sum"))
          .reset_index()
          .sort_values("mes")
    )

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**# de solicitudes por mes**")
        fig = px.line(
            mensual, x="mes", y="cantidad", color="sector", markers=True,
            color_discrete_map=SECTOR_COLORS, height=350,
        )
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        st.markdown("**Monto $ por mes**")
        fig = px.line(
            mensual, x="mes", y="monto", color="sector", markers=True,
            color_discrete_map=SECTOR_COLORS, height=350,
        )
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0),
                          yaxis_tickformat=",.0f")
        st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Vista 3: Top clientes y ejecutivos
# ---------------------------------------------------------------------------
def render_tops(df: pd.DataFrame) -> None:
    st.subheader("🏆 Top clientes y ejecutivos")
    if df.empty:
        st.info("Sin datos para mostrar.")
        return

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Top 10 clientes por monto**")
        top_cli = (
            df.groupby("cliente_nombre")
              .agg(monto=("monto", "sum"), solicitudes=("id", "count"))
              .reset_index()
              .sort_values("monto", ascending=False)
              .head(10)
        )
        fig = px.bar(
            top_cli, x="monto", y="cliente_nombre", orientation="h",
            text="solicitudes", height=400,
            labels={"cliente_nombre": "", "monto": "Monto ($)"},
        )
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0),
                          yaxis={"categoryorder": "total ascending"},
                          xaxis_tickformat=",.0f")
        fig.update_traces(texttemplate="%{text} sols")
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        st.markdown("**Top 10 ejecutivos por # solicitudes**")
        top_ej = (
            df.groupby("ejecutivo")
              .agg(solicitudes=("id", "count"), monto=("monto", "sum"))
              .reset_index()
              .sort_values("solicitudes", ascending=False)
              .head(10)
        )
        fig = px.bar(
            top_ej, x="solicitudes", y="ejecutivo", orientation="h",
            text="solicitudes", height=400,
            labels={"ejecutivo": "", "solicitudes": "Solicitudes"},
            hover_data={"monto": ":$,.0f"},
        )
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0),
                          yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Vista 4: Buscador de solicitudes
# ---------------------------------------------------------------------------
def render_buscador(df: pd.DataFrame) -> None:
    st.subheader("🔎 Buscador de solicitudes")
    if df.empty:
        st.info("Sin solicitudes que mostrar.")
        return

    # Búsqueda libre adicional
    busqueda = st.text_input(
        "Buscar (ID, palanca, ejecutivo, cliente, NIT, doc SAP)",
        placeholder="Ej: NC-2026-00007 o 'Diana' o 'Super Oriente'",
    )
    show = df.copy()
    if busqueda:
        q = busqueda.upper()
        mask = (
            show["id"].str.upper().str.contains(q, na=False)
            | show["ejecutivo"].str.upper().str.contains(q, na=False)
            | show["cliente_nombre"].str.upper().str.contains(q, na=False)
            | show["cliente_nit"].astype(str).str.contains(q, na=False)
            | show["palanca"].str.upper().str.contains(q, na=False)
            | show["sap_docs"].astype(str).str.upper().str.contains(q, na=False)
        )
        show = show[mask]

    # Orden estable para que la selección por clic mapee al ID correcto
    show = show.sort_values("recibido", ascending=False).reset_index(drop=True)

    # Columnas visibles (incluye Texto Cuenta Puente, uno por consecutivo)
    cols_view = ["id", "status", "ejecutivo", "cliente_nombre", "sector",
                 "tipo_nota", "texto_cuenta_puente", "monto", "recibido", "aprobado",
                 "sap_docs", "error"]
    # Compatibilidad: si el snapshot viejo no trae la columna, la creamos vacía
    show = show.copy()
    if "texto_cuenta_puente" not in show.columns:
        show["texto_cuenta_puente"] = ""
    show_view = show[cols_view].rename(columns={
        "id": "ID", "status": "Estado", "ejecutivo": "Ejecutivo",
        "cliente_nombre": "Cliente", "sector": "Sector",
        "tipo_nota": "Tipo nota", "texto_cuenta_puente": "Texto Cuenta Puente",
        "monto": "Monto ($)", "recibido": "Recibido", "aprobado": "Aprobado",
        "sap_docs": "Doc SAP", "error": "Error",
    })

    col_cfg = {
        "Monto ($)": st.column_config.NumberColumn(format="$%,.0f"),
        "Recibido": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm"),
        "Aprobado": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm"),
        "Texto Cuenta Puente": st.column_config.TextColumn(width="large"),
    }

    st.caption("💡 Haz clic en una fila para ver el detalle abajo.")
    sel_id = None
    try:
        event = st.dataframe(
            show_view, use_container_width=True, height=430, hide_index=True,
            on_select="rerun", selection_mode="single-row",
            key="tabla_busqueda", column_config=col_cfg,
        )
        rows = getattr(getattr(event, "selection", None), "rows", []) or []
        if rows:
            sel_id = show.iloc[rows[0]]["id"]
    except TypeError:
        # Streamlit antiguo sin selección por clic: tabla normal
        st.dataframe(
            show_view, use_container_width=True, height=430, hide_index=True,
            column_config=col_cfg,
        )

    # Respaldo / alternativa: seleccionar por ID
    manual = st.selectbox(
        "…o búscala por ID:",
        options=[""] + list(show["id"].values),
        index=0,
    )
    if manual:
        sel_id = manual

    # Detalle de una solicitud
    st.markdown("**Detalle de solicitud**")
    if not sel_id:
        st.info("👆 Selecciona una solicitud (clic en la fila o por ID) para ver su detalle.")
    if sel_id:
        req_row = show[show["id"] == sel_id].iloc[0]
        c1, c2, c3 = st.columns(3)
        c1.markdown(
            f"**ID:** {req_row['id']}<br>"
            f"**Estado:** {req_row['status']}<br>"
            f"**Sector:** {req_row['sector']}<br>"
            f"**Tipo:** {req_row['tipo_nota']}",
            unsafe_allow_html=True,
        )
        c2.markdown(
            f"**Ejecutivo:** {req_row['ejecutivo']}<br>"
            f"**Cliente:** {req_row['cliente_nombre']}<br>"
            f"**NIT:** {req_row['cliente_nit']}<br>"
            f"**Monto:** ${req_row['monto']:,.0f}",
            unsafe_allow_html=True,
        )
        c3.markdown(
            f"**Recibido:** {req_row['recibido']}<br>"
            f"**Aprobado:** {req_row['aprobado'] or '—'}<br>"
            f"**Batch:** {req_row['batch_id'] or '—'}<br>"
            f"**Doc SAP:** {req_row['sap_docs'] or '—'}",
            unsafe_allow_html=True,
        )
        if req_row.get("texto_cuenta_puente"):
            st.markdown(f"**Texto Cuenta Puente:** {req_row['texto_cuenta_puente']}")
        if req_row["palanca"]:
            st.markdown(f"**Palanca:** {req_row['palanca']}")
        if req_row["error"]:
            st.error(f"**Error:** {req_row['error']}")

        st.markdown("**Detalle de líneas**")
        lines = load_lines_df(sel_id)
        if lines.empty:
            st.warning("Sin lineas de detalle.")
        else:
            st.dataframe(
                lines, use_container_width=True, hide_index=True,
                column_config={
                    "valor_sin_iva": st.column_config.NumberColumn(format="$%,.0f"),
                },
            )

    # Export
    st.markdown("---")
    col1, col2 = st.columns([1, 5])
    with col1:
        if not show.empty:
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                show_view.to_excel(writer, sheet_name="Solicitudes", index=False)
            st.download_button(
                "📥 Descargar Excel",
                data=buffer.getvalue(),
                file_name=f"nc_workflow_{datetime.now():%Y%m%d_%H%M}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
    with col2:
        st.caption(f"Exportando {len(show)} solicitud(es) con los filtros actuales.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    st.title("📊 NC Workflow - Dashboard")
    st.caption(
        "Historial de solicitudes de notas credito | "
        f"Ultima actualizacion: {datetime.now():%Y-%m-%d %H:%M}"
    )

    try:
        df = load_requests_df()
    except Exception as e:
        st.error(f"Error cargando datos: {e}")
        st.info("Verifica que la BD state.sqlite exista y que estes en la raiz del proyecto.")
        return

    if df.empty:
        st.warning(
            "Aun no hay solicitudes en la BD. Procesa al menos un correo con "
            "`python main.py step ingest && python main.py step consolidate` "
            "y refresca esta pagina."
        )
        return

    df = render_filters(df)

    render_kpis(df)
    st.markdown("---")

    tab1, tab2, tab3, tab4 = st.tabs([
        "📦 Sector y estado",
        "📈 Tendencia mensual",
        "🏆 Top clientes/ejecutivos",
        "🔎 Buscador detallado",
    ])

    with tab1:
        render_sector_estado(df)
    with tab2:
        render_tendencia(df)
    with tab3:
        render_tops(df)
    with tab4:
        render_buscador(df)


if __name__ == "__main__":
    main()