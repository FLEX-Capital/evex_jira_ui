import colorsys

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import reporting_policy as reporting

MODE_ABS = "Absolute Zahlen"
MODE_LOG = "Absolut (log)"
MODE_REL = "Relativ (%)"


def generate_distinct_colors(n):
    # evenly spaced hues in HSV space
    hues = np.linspace(0, 1, n, endpoint=False)
    colors = []
    for h in hues:
        r, g, b = colorsys.hsv_to_rgb(h, 0.65, 0.9)
        colors.append(f"rgb({int(r*255)},{int(g*255)},{int(b*255)})")
    return colors


def create_resolution_time_charts(df: pd.DataFrame) -> list[go.Figure]:
    """Build duration charts accounting for every canonical completed ticket."""
    if "completion_state" not in df or df["completion_state"].isna().any():
        from data_transformation import enrich_jira_time_metrics

        df = enrich_jira_time_metrics(df)
    states = df["completion_state"].astype("string").fillna("unknown")
    counts = {
        "completed": int((states == "done").sum()),
        "open_excluded": int((states == "open").sum()),
        "unknown_completion_excluded": int((~states.isin(["done", "open"])).sum()),
    }
    completed = df.loc[states == "done"].copy()
    bin_col = "time_to_resolution_bin"
    bins = completed.get(bin_col, pd.Series(index=completed.index, dtype="string"))
    bins = bins.astype("string").fillna("Unbekannte Dauer")
    bins = bins.where(bins.isin(reporting.ELAPSED_ORDER), "Unbekannte Dauer")
    bin_order = list(reporting.ELAPSED_ORDER)
    completed[bin_col] = pd.Categorical(bins, categories=bin_order, ordered=True)

    totals = completed.groupby(bin_col, observed=False)["key"].count()
    y_max = max(1, totals.max() if not totals.empty else 0) * 1.15
    figures = []
    for group_col, group_label in [
        (None, None),
        ("request_type", "Quelle"),
        ("source", "Ursprung Ticket"),
    ]:
        group_cols = [bin_col]
        if group_col:
            # Include unset fields so all three charts retain the same totals.
            values = completed.get(
                group_col, pd.Series(index=completed.index, dtype="object")
            )
            completed[group_col] = (
                values.astype("string").fillna("").str.strip().replace("", "Unbekannt")
            )
            group_cols.append(group_col)

        result = (
            completed.groupby(group_cols, observed=False)["key"].count().reset_index()
        )
        title = "Anzahl Fertige Tickets nach Bearbeitungszeit"
        if group_label:
            title += f" – {group_label}"
        fig = px.bar(
            result,
            x=bin_col,
            y="key",
            color=group_col,
            category_orders={bin_col: bin_order},
            labels={
                bin_col: "Bearbeitungszeit in Stunden",
                "key": "Anzahl Fertige Tickets",
                **({group_col: group_label} if group_col else {}),
            },
            title=title,
        )
        fig = apply_font(fig)
        fig.update_layout(barmode="stack", margin=dict(l=100, r=40, b=80))
        fig.update_xaxes(
            type="category",
            categoryorder="array",
            categoryarray=bin_order,
            range=[-0.5, max(len(bin_order) - 0.5, 0.5)],
        )
        fig.update_yaxes(range=[0, y_max])
        fig.update_layout(meta=counts)
        fig.add_annotation(
            text=(
                f"Abgeschlossen: {counts['completed']}; "
                f"offen ausgeschlossen: {counts['open_excluded']}; "
                f"Abschluss unbekannt ausgeschlossen: {counts['unknown_completion_excluded']}"
            ),
            x=0,
            y=1.15,
            xref="paper",
            yref="paper",
            showarrow=False,
        )
        figures.append(fig)
    return figures


def create_toggle_chart(
    df,
    x_col,
    group_col,
    count_col="key",
    x_label=None,
    toggle_key="default_key",
    color_map=None,
    force_bottom_value=None,
    group_order=None,
    sort_x_by_total=False,
    plot_height=400,
    plot_width=None,
    allow_log=False,
):
    """
    Generates a stacked bar chart with:
    - Toggle between Absolute/Relative view
    - Percentages inside bars
    - Totals on top of bars
    - Custom ordering and coloring
    """

    # 1. Data Preparation
    # Group and count
    df = df.copy()
    for column in dict.fromkeys([x_col, group_col]):
        df[column] = (
            df[column].astype("string").fillna("").str.strip().replace("", "Unbekannt")
        )
    result = (
        df[[x_col, group_col, count_col]]
        .groupby([x_col, group_col])
        .count()
        .reset_index()
    )

    # Calculate Totals & Percentages per stack
    total_per_group = result.groupby(x_col)[count_col].transform("sum")
    result["percentage"] = result[count_col] / total_per_group

    # Custom Labels: "Count <br> (Percent)"
    result["custom_label"] = result.apply(
        lambda x: f"{x[count_col]}<br>({x['percentage']:.0%})", axis=1
    )

    # 2. Ordering Logic
    # Group Order (Stack Order)
    present = result[group_col].unique().tolist()
    if group_order:
        # Caller-supplied order (e.g. a colour ramp that only reads correctly
        # in sequence). Without it the stack follows whatever order groupby
        # happened to produce, which is alphabetical by label, not meaningful.
        known = [value for value in group_order if value in present]
        group_order = known + [value for value in present if value not in known]
    else:
        group_order = present
    if force_bottom_value and force_bottom_value in group_order:
        group_order.remove(force_bottom_value)
        group_order.insert(0, force_bottom_value)  # Insert at 0 to put at bottom

    # X-Axis Order
    if sort_x_by_total:
        # Sort X-axis based on total count descending
        total_counts_index = (
            result.groupby(x_col)[count_col].sum().sort_values(ascending=False).index
        )
    else:
        total_counts_index = None  # Use default or existing order

    # 3. Calculate Column Totals (for the label on top)
    column_totals = result.groupby(x_col)[count_col].sum().reset_index()

    # 4. Streamlit Toggle
    modes = [MODE_ABS, MODE_LOG, MODE_REL] if allow_log else [MODE_ABS, MODE_REL]
    mode = st.radio(
        "Ansicht wählen:",
        modes,
        horizontal=True,
        index=0,
        key=toggle_key,
    )

    # 5. Configure Variables based on Toggle
    if mode in (MODE_ABS, MODE_LOG):
        y_val = count_col
        y_title = "Anzahl Tickets"
        y_format = None
        # Labels for top of bars
        y_text_pos = column_totals[count_col]
        text_content = column_totals[count_col].astype(str)
        # Add 15% buffer
        y_max = column_totals[count_col].max() * 1.15
    else:
        y_val = "percentage"
        y_title = "Prozentualer Anteil"
        y_format = ".0%"
        # Labels for top (100%)
        y_text_pos = [1] * len(column_totals)
        text_content = column_totals[count_col].apply(lambda x: f"Total: {x}")
        y_max = 1.15

    # 6. Plot Base Chart
    fig = px.bar(
        result,
        x=x_col,
        y=y_val,
        color=group_col,
        text="custom_label",
        color_discrete_map=color_map,
        category_orders={group_col: group_order, x_col: total_counts_index},
    )

    # 7. Styling (Must happen BEFORE adding Scatter trace)
    fig.update_traces(textposition="inside", insidetextanchor="middle")

    # Assuming apply_font is available globally
    try:
        fig = apply_font(fig)
    except NameError:
        pass  # Skip if function not found

    # 8. Add Totals on Top (Scatter Trace)
    fig.add_trace(
        go.Scatter(
            x=column_totals[x_col],
            y=y_text_pos,
            text=text_content,
            mode="text",
            textposition="top center",
            textfont=dict(size=12, color="black", weight="bold"),
            showlegend=False,
            hoverinfo="skip",
        )
    )

    # 9. Final Layout
    fig.update_xaxes(title_text=x_label if x_label else x_col)
    if mode == MODE_LOG:
        # A log axis cannot start at 0, so the [0, y_max] range must not be set.
        fig.update_yaxes(title_text=y_title, tickformat=y_format, type="log")
        fig.add_annotation(
            text="Log-Skala – Balkenanteile sind nicht proportional lesbar",
            xref="paper",
            yref="paper",
            x=0,
            y=1.06,
            showarrow=False,
            font=dict(size=12, color="#B00020"),
        )
    else:
        fig.update_yaxes(title_text=y_title, tickformat=y_format, range=[0, y_max])
    # Plotly titles the legend with the color column's name; the swatches are
    # self-explanatory, so drop it.
    fig.update_layout(legend_title_text="")

    st.plotly_chart(fig, height=plot_height, width=plot_width)


def apply_font(fig, base=20):
    fig.update_layout(font=dict(size=base))
    fig.update_xaxes(title_font=dict(size=base), tickfont=dict(size=base - 2))
    fig.update_yaxes(title_font=dict(size=base), tickfont=dict(size=base - 2))

    fig.update_traces(
        textfont_size=base - 2,
        textposition="inside",
        insidetextanchor="middle",
        textangle=0,
    )
    fig.update_layout(
        uniformtext_minsize=base - 2,  # minimum label size
        uniformtext_mode="show",  # do NOT hide or shrink
    )
    fig.update_layout(
        legend=dict(title=dict(font=dict(size=base)), font=dict(size=base - 2))
    )
    fig.update_layout(
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )
    return fig


def apply_filters(df):
    with st.sidebar:
        status = st.multiselect("Status", df.status.unique())
        assignee = st.multiselect("Assignee", df.assignee.unique())

    if status:
        df = df[df.status.isin(status)]
    if assignee:
        df = df[df.assignee.isin(assignee)]

    return df
