import pandas as pd
import requests
from datetime import datetime
import pytz
import streamlit as st
import plotly.express as px
from datetime import datetime, timedelta
from streamlit_autorefresh import st_autorefresh


# ==============================
# CONFIGURACIÓN STREAMLIT
# ==============================
st.set_page_config(
    page_title="Telemetría NIA",
    layout="wide",
    initial_sidebar_state="expanded"
)

st_autorefresh(
    interval=1 * 60 * 1000,  # 10 minutos en ms
    limit=None,
    key="auto_refresh"
)

# ==============================
# CSS PERSONALIZADO
# ==============================
with open("style.css", encoding="utf-8") as f:
    css = f.read()

st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)

# ==============================
# CONFIGURACIÓN Y PARÁMETROS
# ==============================
BASE_URL = st.secrets["BASE_URL"]
USERNAME = st.secrets["USERNAME"]
PASSWORD = st.secrets["PASSWORD"]
ASSET_ID = st.secrets["ASSET_ID"]

KEYS = [
    "logs_nia","logs_ubicacion",
    "shared_tipo","shared_placaTracto","shared_placaPlataforma",
    "shared_tracker","shared_conductor","shared_empresa"
]
tz_pe = pytz.timezone("America/Lima")

# Últimos 30 días
end_ts = int(datetime.now().timestamp() * 1000)
start_ts = end_ts - 30*24*60*60*1000

# ==============================
# FUNCIÓN PARA CARGAR DATOS
# ==============================
@st.cache_data(show_spinner=True, ttl=500)
def cargar_datos():
    # -------- LOGIN --------
    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=3))
    try:
        login = session.post(
            f"{BASE_URL}/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
            timeout=15
        )
        login.raise_for_status()
        token = login.json()["token"]
        session.headers.update({"X-Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    except requests.RequestException as e:
        st.error(f"Error en login: {e}")
        return pd.DataFrame()

    # -------- TELEMETRÍA --------
    url = (
        f"{BASE_URL}/api/plugins/telemetry/ASSET/{ASSET_ID}/values/timeseries"
        f"?keys={','.join(KEYS)}"
        f"&startTs={start_ts}&endTs={end_ts}"
        f"&agg=NONE&order=ASC&limit=100000"
    )
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            st.warning("No se encontraron eventos")
            return pd.DataFrame()
    except requests.RequestException as e:
        st.error(f"Error al obtener telemetría: {e}")
        return pd.DataFrame()

    # -------- NORMALIZAR JSON A DATAFRAME --------
    dfs = []
    for key, values in data.items():
        if not values:
            continue
        df_key = pd.DataFrame(values)
        df_key.rename(columns={"value": key}, inplace=True)
        dfs.append(df_key[["ts", key]])
    df_all = pd.concat(dfs).groupby("ts", as_index=False).first()
    df_all.rename(columns={"ts": "evento_ts"}, inplace=True)
    df_all["evento_fecha"] = pd.to_datetime(df_all["evento_ts"], unit="ms", utc=True)\
                                .dt.tz_convert("America/Lima").dt.tz_localize(None)

    df = df_all.copy()
    df["evento_ts"] = pd.to_numeric(df["evento_ts"], errors="coerce")
    df = df.dropna(subset=["logs_nia", "evento_ts"])

    # -------- RELLENAR DATOS DESASIGNACIÓN --------
    cols_a_rellenar = [
        "shared_tipo","shared_placaTracto","shared_placaPlataforma",
        "shared_tracker","shared_conductor","shared_empresa"
    ]
    df_desasig = df[df["logs_ubicacion"]=="Desasignación"][["logs_nia"] + cols_a_rellenar]\
                    .drop_duplicates(subset="logs_nia")
    df = df.merge(df_desasig, on="logs_nia", how="left", suffixes=('', '_desasig'))
    for col in cols_a_rellenar:
        df[col] = df[col].fillna(df[f"{col}_desasig"])
    df.drop(columns=[f"{col}_desasig" for col in cols_a_rellenar], inplace=True)

    # -------- NORMALIZAR UBICACIONES --------
    mapa_ubicaciones = {"Calificacion": "Calificación", "Iman Core": "Imán"}
    df["logs_ubicacion"] = df["logs_ubicacion"].replace(mapa_ubicaciones)

    # -------- VALIDAR RECORRIDOS COMPLETOS --------
    def recorrido_completo(t):
        ts_ingreso = t.loc[t["logs_ubicacion"]=="En Asignación","evento_ts"]
        ts_salida = t.loc[t["logs_ubicacion"]=="Desasignación","evento_ts"]
        return (not ts_ingreso.empty) and (not ts_salida.empty) and (ts_ingreso.min() < ts_salida.max())

    nias_validos = df.groupby("logs_nia", sort=False).filter(recorrido_completo)["logs_nia"].unique()
    df = df[df["logs_nia"].isin(nias_validos)]

    # -------- CALCULO TIEMPOS DE PERMANENCIA --------
    df_ing = df[df["logs_ubicacion"]=="En Asignación"].groupby("logs_nia")["evento_ts"].min()
    df_sal = df[df["logs_ubicacion"]=="Desasignación"].groupby("logs_nia")["evento_ts"].max()
    df_tiempos = pd.concat([df_ing, df_sal], axis=1).dropna().reset_index()
    df_tiempos.columns = ["logs_nia","ts_ingreso","ts_salida"]
    df_tiempos["tiempo_permanencia"] = (df_tiempos["ts_salida"] - df_tiempos["ts_ingreso"])/1000/3600
    df_tiempos["ingreso"] = pd.to_datetime(df_tiempos["ts_ingreso"], unit="ms", utc=True).dt.tz_convert(tz_pe).dt.tz_localize(None)
    df_tiempos["salida"] = pd.to_datetime(df_tiempos["ts_salida"], unit="ms", utc=True).dt.tz_convert(tz_pe).dt.tz_localize(None)

    # -------- ORDENAR Y CALCULAR TIEMPOS ENTRE EVENTOS --------
    df = df.sort_values(["logs_nia","evento_ts"]).assign(
        evento_ts_siguiente=lambda x: x.groupby("logs_nia")["evento_ts"].shift(-1),
        tiempo_min=lambda x: (x["evento_ts_siguiente"] - x["evento_ts"])/1000/60
    )
    df = df[df["tiempo_min"].notna() & (df["tiempo_min"]>=0)]

    # -------- RENOMBRAR BALANZA Y RUTAS --------
    df["logs_ubicacion_renombrada"] = df["logs_ubicacion"]
    for nia, grupo in df.groupby("logs_nia"):
        grupo = grupo.sort_values("evento_ts")
        balanza = grupo[grupo["logs_ubicacion"]=="Balanza"]
        if not balanza.empty:
            balanza_ini_idx = balanza.index[0]
            balanza_fin_idx = balanza.index[-1]
            df.loc[balanza_ini_idx,"logs_ubicacion_renombrada"] = "Balanza inicial"
            df.loc[balanza_fin_idx,"logs_ubicacion_renombrada"] = "Balanza final"
            ruta_ini = grupo[grupo["evento_ts"] < grupo.loc[balanza_ini_idx,"evento_ts"]]
            if not ruta_ini.empty and ruta_ini.iloc[-1]["logs_ubicacion"]=="Ruta hacia Balanza":
                df.loc[ruta_ini.index[-1],"logs_ubicacion_renombrada"] = "Ruta hacia Balanza inicial"
            ruta_fin = grupo[grupo["evento_ts"] < grupo.loc[balanza_fin_idx,"evento_ts"]]
            if not ruta_fin.empty and ruta_fin.iloc[-1]["logs_ubicacion"]=="Ruta hacia Balanza":
                df.loc[ruta_fin.index[-1],"logs_ubicacion_renombrada"] = "Ruta hacia Balanza final"

    # -------- PIVOT FINAL Y DATOS PARA GRAFICOS --------
    cols_shared = [
        "logs_nia","shared_tipo","shared_placaTracto","shared_placaPlataforma",
        "shared_tracker","shared_conductor","shared_empresa"
    ]
    df_shared = df[cols_shared].drop_duplicates(subset=["logs_nia"])
    df_pivot = df.groupby(["logs_nia","logs_ubicacion_renombrada"], observed=True)["tiempo_min"].sum().reset_index()
    df_pivot_final = df_pivot.pivot_table(
        index="logs_nia", columns="logs_ubicacion_renombrada", values="tiempo_min", fill_value=0
    ).reset_index()
    df_pivot_final = df_pivot_final.merge(df_shared, on="logs_nia", how="left")
    df_pivot_final = df_pivot_final.merge(df_tiempos, on="logs_nia", how="left")

    # ======================================================
    # TIEMPO DESCARGA
    # ======================================================

    cols_descarga = [
        "Balanza","Balanza final","Balanza inicial","Barrido",
        "Calificacion","Calificación","Consumo","Desasignación",
        "Descarga","Desmanteo","Embutición","Iman Core","Imán",
        "Oxicorte","Ruta hacia Balanza","Ruta hacia Balanza final",
        "Ruta hacia Balanza inicial","Ruta hacia Barrido",
        "Ruta hacia Calificacion","Ruta hacia Calificación",
        "Ruta hacia Consumo","Ruta hacia Descarga",
        "Ruta hacia Desmanteo","Ruta hacia Embutición",
        "Ruta hacia Imán","Ruta hacia Oxicorte"
    ]

    cols_existentes = [c for c in cols_descarga if c in df_pivot_final.columns]

    df_pivot_final["tiempo_descarga"] = df_pivot_final[cols_existentes].sum(axis=1)/60

    # ======================================================
    # DataFrame final listo para graficar (simplificado)
    # ======================================================
    cols_base = [
        "logs_nia","Balanza final","Balanza inicial","Barrido","Calificación","Consumo",
        "Descarga","Desmanteo","Embutición","Imán","Oxicorte",
        "Ruta hacia Balanza final","Ruta hacia Balanza inicial",
        "Ruta hacia Barrido","Ruta hacia Calificación","Ruta hacia Consumo",
        "Ruta hacia Descarga","Ruta hacia Desmanteo","Ruta hacia Embutición",
        "Ruta hacia Imán","Ruta hacia Oxicorte",
        "shared_tipo","shared_placaTracto","shared_placaPlataforma",
        "shared_tracker","shared_conductor",
        "shared_empresa",
        "ingreso","salida","tiempo_permanencia","tiempo_descarga"
    ]

    rename = {
        "logs_nia": "NIA","shared_tipo": "Tipo","shared_placaTracto": "Placa Tracto",
        "shared_placaPlataforma": "Placa Plataforma","shared_tracker": "Tracker",
        "shared_conductor": "Conductor","shared_empresa": "Empresa",
        "ingreso": "Ingreso","salida": "Salida",
        "tiempo_permanencia": "T. Permanencia (h)","tiempo_descarga": "T. Descarga (h)",
        "Ruta hacia ": "Ruta "
    }

    df_graficos = (
        df_pivot_final
        .loc[:, [c for c in cols_base if c in df_pivot_final.columns]]
        .rename(columns=lambda c: rename.get(c, c.replace("Ruta hacia ", "Ruta ")))
    )

    orden = [
        "NIA","Tipo","Empresa","Ingreso","Salida","T. Permanencia (h)","T. Descarga (h)",
        "Ruta Desmanteo","Desmanteo",
        "Ruta Balanza inicial","Balanza inicial",
        "Ruta Calificación","Calificación",
        "Ruta Descarga","Descarga",
        "Ruta Imán","Imán",
        "Ruta Barrido","Barrido",
        "Ruta Balanza final","Balanza final",
        "Ruta Consumo","Consumo",
        "Ruta Embutición","Embutición",
        "Oxicorte","Ruta Oxicorte",
        "Placa Tracto","Placa Plataforma","Tracker",
        "Conductor"
    ]

    df_graficos = df_graficos.loc[:, [c for c in orden if c in df_graficos.columns]]

    cols_tiempo = df_graficos.select_dtypes("number").columns
    df_graficos[cols_tiempo] = df_graficos[cols_tiempo].round(2)

    return df_graficos

# ==============================
# CARGAR DATOS Y FILTRAR NIA
# ==============================
df_graficos = cargar_datos()

if df_graficos.empty:
    st.stop()

# Filtrar NIA válidos: numéricos, rango 2000000000 - 2999999999
df_graficos["NIA"] = pd.to_numeric(df_graficos["NIA"], errors='coerce')
df_graficos = df_graficos[
    df_graficos["NIA"].notna() & 
    (df_graficos["NIA"] >= 2000000000) & 
    (df_graficos["NIA"] <= 2999999999)
]

if df_graficos.empty:
    st.warning("No hay NIA válidos dentro del rango especificado.")
    st.stop()

# Filtrar columnas de tiempo
cols_tiempos = [c for c in df_graficos.columns if c not in [
    "NIA","Tipo","Placa Tracto","Placa Plataforma","Tracker",
    "Conductor","Empresa",
    "Ingreso","Salida","T. Permanencia (h)","T. Descarga (h)"
]]
df_graficos[cols_tiempos] = df_graficos[cols_tiempos].apply(pd.to_numeric, errors='coerce')

# ==============================
# SIDEBAR TIPO PESTAÑAS + FILTROS
# ==============================

# Inicializar variable de sesión si no existe
if "pagina" not in st.session_state:
    st.session_state.pagina = "Tiempos promedio de zona"

# Función para cambiar de página
def cambiar_pagina(pagina):
    st.session_state.pagina = pagina

# Botones de sidebar (tipo pestañas)
if st.sidebar.button("Reporte"):
    cambiar_pagina("Reporte")
if st.sidebar.button("Recorridos"):
    cambiar_pagina("Recorridos")
if st.sidebar.button("Tiempos promedio de zona"):
    cambiar_pagina("Tiempos promedio de zona")
if st.sidebar.button("Detalle Zonas"):
    cambiar_pagina("Detalle Zonas")

tz_pe = pytz.timezone("America/Lima")

# --------------------------------------------------
# Convertir "Salida" a datetime con timezone
# --------------------------------------------------
df_graficos["Salida"] = pd.to_datetime(df_graficos["Salida"], errors="coerce")
df_graficos["Salida"] = df_graficos["Salida"].dt.tz_localize(
    "America/Lima",
    ambiguous="NaT",
    nonexistent="shift_forward"
)

# --------------------------------------------------
# Sidebar
# --------------------------------------------------
st.sidebar.title("Navegación y filtros")

filtro_opcion = st.sidebar.selectbox(
    "Filtrar por Fecha / Turno",
    [
        "Turno actual",
        "Turno anterior",
        "Últimas 6 horas",
        "Últimas 12 horas",
        "Últimas 24 horas",
        "Última semana",
        "Último mes",
        "Todos"
    ]
)

# --------------------------------------------------
# Fecha actual
# --------------------------------------------------
ahora = datetime.now(tz_pe)
hora = ahora.hour

# --------------------------------------------------
# Calcular turnos
# --------------------------------------------------
if 8 <= hora < 20:
    # Turno día
    inicio_turno_actual = ahora.replace(hour=8, minute=0, second=0, microsecond=0)
    fin_turno_actual = ahora.replace(hour=20, minute=0, second=0, microsecond=0)
else:
    # Turno noche
    if hora >= 20:
        inicio_turno_actual = ahora.replace(hour=20, minute=0, second=0, microsecond=0)
    else:
        inicio_turno_actual = (ahora - timedelta(days=1)).replace(
            hour=20, minute=0, second=0, microsecond=0
        )
    fin_turno_actual = inicio_turno_actual + timedelta(hours=12)

inicio_turno_anterior = inicio_turno_actual - timedelta(hours=12)
fin_turno_anterior = inicio_turno_actual

# --------------------------------------------------
# Aplicar filtro único
# --------------------------------------------------
if filtro_opcion == "Turno actual":
    df_graficos = df_graficos[
        (df_graficos["Salida"] >= inicio_turno_actual) &
        (df_graficos["Salida"] < fin_turno_actual)
    ]

elif filtro_opcion == "Turno anterior":
    df_graficos = df_graficos[
        (df_graficos["Salida"] >= inicio_turno_anterior) &
        (df_graficos["Salida"] < fin_turno_anterior)
    ]

elif filtro_opcion == "Últimas 6 horas":
    df_graficos = df_graficos[df_graficos["Salida"] >= ahora - timedelta(hours=6)]

elif filtro_opcion == "Últimas 12 horas":
    df_graficos = df_graficos[df_graficos["Salida"] >= ahora - timedelta(hours=12)]

elif filtro_opcion == "Últimas 24 horas":
    df_graficos = df_graficos[df_graficos["Salida"] >= ahora - timedelta(days=1)]

elif filtro_opcion == "Última semana":
    df_graficos = df_graficos[df_graficos["Salida"] >= ahora - timedelta(weeks=1)]

elif filtro_opcion == "Último mes":
    df_graficos = df_graficos[df_graficos["Salida"] >= ahora - timedelta(days=30)]

# --------------------------------------------------
# Validación final
# --------------------------------------------------
if df_graficos.empty:
    st.warning("No hay datos para el filtro seleccionado.")
    st.stop()
# ==============================
# PÁGINA: Dashboard
# ==============================
pagina = st.session_state.pagina
if pagina == "Reporte":
    st.title("Reporte de Recorridos")
    st.metric("Total NIA", len(df_graficos["NIA"].unique()))
    st.metric("Tipos de unidad", len(df_graficos["Tipo"].unique()))

# ==============================
# PÁGINA: Tabla completa
# ==============================
elif pagina == "Recorridos":
    st.subheader("Tabla completa de recorridos")

    df_tabla = (
        df_graficos
        .sort_values("Salida", ascending=False)
        .reset_index(drop=True)
    )

    st.dataframe(df_tabla, width='stretch')
# ==============================
# PÁGINA: Gráficos por tipo de unidad
# ==============================
elif pagina == "Tiempos promedio de zona":
    st.subheader("Tiempo promedio por Zona y Tipo")

    # ============================
    # PREPARAR DATOS
    # ============================
    df_tipo_prom = (
        df_graficos
        .groupby("Tipo")[cols_tiempos]
        .mean()
        .reset_index()
    )

    df_tipo_long = df_tipo_prom.melt(
        id_vars="Tipo",
        var_name="Ubicación",
        value_name="Promedio_minutos"
    )

    # Orden real del eje X
    orden_x = df_tipo_long["Ubicación"].unique().tolist()

    # ============================
    # GRÁFICO
    # ============================
    fig_tipo = px.bar(
        df_tipo_long,
        x="Ubicación",
        y="Promedio_minutos",
        color="Tipo",
        barmode="group",
        labels={
            "Ubicación": "Ubicación",
            "Promedio_minutos": "Tiempo promedio (minutos)"
        },
        text_auto=".0f",
        color_discrete_map={
            "Plataforma": "steelblue",
            "Tolva": "orange",
        }
    )

    fig_tipo.update_traces(
        textposition="outside",
        textfont=dict(size=14)
    )

    fig_tipo.update_xaxes(
        tickfont=dict(size=14),
        title_font=dict(size=14)
    )

    fig_tipo.update_yaxes(
        tickfont=dict(size=12),
        title_font=dict(size=14)
    )

    fig_tipo.update_layout(
        legend=dict(
            font=dict(size=12),
            title_font=dict(size=13)
        )
    )

    fig_tipo.update_traces(textposition="outside")

    # ============================
    # ESPACIO PARA ETIQUETAS
    # ============================
    max_y = df_tipo_long["Promedio_minutos"].max()

    fig_tipo.update_yaxes(
        range=[0, max_y * 1.25],
        automargin=True
    )

    # ============================
    # BANDAS DE FONDO
    # ============================
    def banda(x0, x1, color):
        return dict(
            type="rect",
            xref="x",
            yref="paper",
            x0=x0 - 0.5,
            x1=x1 + 0.5,
            y0=0,
            y1=0.8,
            fillcolor=color,
            opacity=0.20,
            layer="below",
            line_width=0
        )

    shapes = []

    if "Ruta Calificación" in orden_x and "Descarga" in orden_x:
        shapes.append(
            banda(
                orden_x.index("Ruta Calificación"),
                orden_x.index("Descarga"),
                "#FF5B5B"   # azul claro
            )
        )

    # ============================
    # LAYOUT
    # ============================
    fig_tipo.update_layout(
        xaxis_tickangle=-45,
        yaxis_title="Tiempo promedio (minutos)",
        xaxis_title="Ubicación",
        margin=dict(l=20, r=20, t=20, b=120),
        legend=dict(
            title_text=" ",
            orientation="h",
            yanchor="top",
            y=-0.45,
            xanchor="center",
            x=0.5
        ),
        shapes=shapes
    )

    # ============================
    # MOSTRAR
    # ============================
    st.plotly_chart(fig_tipo, width='stretch')

    ubicaciones_clave = [
        "Ruta Calificación",
        "Calificación",
        "Ruta Descarga",
        "Descarga"
    ]
    df_tiempo_destacado = (
        df_tipo_long[df_tipo_long["Ubicación"].isin(ubicaciones_clave)]
        .groupby("Tipo")["Promedio_minutos"]
        .sum()
        .reset_index()
    )


    st.markdown("#### Tiempo destacado")

    cols = st.columns(len(df_tiempo_destacado))

    for col, (_, row) in zip(cols, df_tiempo_destacado.iterrows()):
        col.metric(
            label=f"{row['Tipo']}",
            value=f"{row['Promedio_minutos']:.0f} min"
        )


# ==============================
# PÁGINA: Tiempos promedio por ubicación
# ==============================
elif pagina == "Detalle Zonas":
    
    if cols_tiempos:

        # --------------------------------------------------
        # 1️⃣ ORDEN ÚNICO DE UBICACIONES
        # --------------------------------------------------
        orden_ubicaciones = list(dict.fromkeys(cols_tiempos))

        # --------------------------------------------------
        # LAYOUT: SELECTORES (30%) | TABLA (70%)
        # --------------------------------------------------
        col_filtros, col_tabla = st.columns([2, 8])

        # ==================================================
        # COLUMNA IZQUIERDA – SELECTORES (30%)
        # ==================================================
        with col_filtros:
            st.subheader("Filtros")

            # 2️⃣ SELECTOR DE TIPO
            tipos_disponibles = df_graficos["Tipo"].dropna().unique().tolist()
            selected_tipo = st.selectbox(
                "Seleccione tipo",
                tipos_disponibles
            )

            # 3️⃣ FILTRAR DF POR TIPO
            df_tipo = df_graficos[df_graficos["Tipo"] == selected_tipo]

            # 5️⃣ SELECTOR DE UBICACIÓN
            selected_location = st.selectbox(
                "Seleccione ubicación",
                orden_ubicaciones
            )

        # ==================================================
        # COLUMNA DERECHA – TABLA (70%)
        # ==================================================
        with col_tabla:
            st.subheader("Detalle por NIA")

            # 4️⃣ PROMEDIOS POR UBICACIÓN (SIN ORDENAR)
            prom_ubicacion = df_tipo[orden_ubicaciones].mean().reset_index()
            prom_ubicacion.columns = ["Ubicación", "Promedio_minutos"]

            # 6️⃣ DETALLE POR NIA
            nias_filtradas = (
                df_tipo[["NIA", "Ingreso", "Empresa", selected_location]]
                .rename(columns={selected_location: "Tiempo (min)"})
                .sort_values("Tiempo (min)", ascending=False)
                .reset_index(drop=True)
            )

            promedio_val = prom_ubicacion.loc[
                prom_ubicacion["Ubicación"] == selected_location,
                "Promedio_minutos"
            ].values[0]

            st.write(
                f"Tiempo promedio en **{selected_location}** "
                f"para tipo **{selected_tipo}**: "
                f"{promedio_val:.2f} minutos"
            )

            st.dataframe(
                nias_filtradas,
                width='stretch'
            )

        # --------------------------------------------------
        # 7️⃣ RESPETAR ORDEN EN PLOTLY
        # --------------------------------------------------
        prom_ubicacion["Ubicación"] = pd.Categorical(
            prom_ubicacion["Ubicación"],
            categories=orden_ubicaciones,
            ordered=True
        )

        prom_ubicacion = prom_ubicacion.sort_values("Ubicación")

        prom_ubicacion["highlight"] = (
            prom_ubicacion["Ubicación"] == selected_location
        )

        # --------------------------------------------------
        # 8️⃣ GRÁFICA
        # --------------------------------------------------
        fig = px.bar(
            prom_ubicacion,
            x="Ubicación",
            y="Promedio_minutos",
            color="highlight",
            color_discrete_map={True: "orange", False: "steelblue"},
            labels={
                "Ubicación": "Ubicación",
                "Promedio_minutos": "Tiempo promedio (minutos)"
            },
            text_auto=".1f",
            category_orders={"Ubicación": orden_ubicaciones}
        )

        fig.update_traces(textposition="outside")
        fig.update_layout(
            title=f"Tiempo promedio por ubicación: {selected_tipo}",
            xaxis_title="Ubicación",
            yaxis_title="Tiempo promedio (minutos)",
            showlegend=False
        )

        st.plotly_chart(fig, width='stretch')

    else:
        st.info("No hay columnas de tiempo disponibles para graficar.")


