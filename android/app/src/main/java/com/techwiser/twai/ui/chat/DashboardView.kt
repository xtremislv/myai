package com.techwiser.twai.ui.chat

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.IosShare
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedCard
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.techwiser.twai.data.Chart
import com.techwiser.twai.data.Dashboard
import com.techwiser.twai.data.DataTable
import com.techwiser.twai.ui.shareText
import com.techwiser.twai.ui.theme.LocalExtra
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/** A dashboard under a reply (app/dashboards.py): headline numbers, charts, tables, notes. */
@Composable
fun DashboardView(d: Dashboard, modifier: Modifier = Modifier) {
    OutlinedCard(modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Column {
                Text(d.title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                if (d.subtitle.isNotBlank()) {
                    Text(d.subtitle, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            if (d.kpis.isNotEmpty()) {
                Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    d.kpis.forEach { k ->
                        Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer)) {
                            Column(Modifier.widthIn(min = 120.dp, max = 200.dp).padding(12.dp)) {
                                Text(k.label, style = MaterialTheme.typography.labelMedium,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis)
                                Text(k.value, style = MaterialTheme.typography.headlineSmall, maxLines = 1)
                                if (k.change.isNotBlank()) {
                                    val c = when (k.trend) {
                                        "up" -> LocalExtra.current.good
                                        "down" -> MaterialTheme.colorScheme.error
                                        else -> MaterialTheme.colorScheme.onSurfaceVariant
                                    }
                                    val arrow = when (k.trend) { "up" -> "▲ "; "down" -> "▼ "; else -> "" }
                                    Text(arrow + k.change, style = MaterialTheme.typography.labelSmall, color = c)
                                }
                            }
                        }
                    }
                }
            }
            d.charts.forEach { ChartView(it) }
            d.tables.forEach { TableView(it) }
            if (d.notes.isNotBlank()) Markdown(d.notes)
        }
    }
}

private fun fmt(v: Double): String = when {
    abs(v) >= 1e7 -> "%.1fCr".format(v / 1e7)
    abs(v) >= 1e5 -> "%.1fL".format(v / 1e5)
    abs(v) >= 1e3 -> "%.1fk".format(v / 1e3)
    v == v.toLong().toDouble() -> v.toLong().toString()
    else -> "%.2f".format(v)
}

@Composable
private fun ChartView(chart: Chart) {
    val palette = LocalExtra.current.series
    val grid = MaterialTheme.colorScheme.outlineVariant
    val ink = MaterialTheme.colorScheme.onSurfaceVariant
    var showNumbers by remember { mutableStateOf(false) }
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(chart.title.ifBlank { "Chart" }, style = MaterialTheme.typography.titleSmall, modifier = Modifier.weight(1f))
            if (chart.unit.isNotBlank()) Text(chart.unit, style = MaterialTheme.typography.labelSmall, color = ink)
        }
        val values = chart.series.flatMap { s -> s.values.filterNotNull() }
        val hi = max(values.maxOrNull() ?: 1.0, 0.0)
        val lo = min(values.minOrNull() ?: 0.0, 0.0)
        val span = (hi - lo).takeIf { it > 0 } ?: 1.0
        when (chart.type) {
            "hbar" -> Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                val series = chart.series.firstOrNull()
                chart.labels.forEachIndexed { i, label ->
                    val v = series?.values?.getOrNull(i) ?: 0.0
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(label, style = MaterialTheme.typography.labelMedium, maxLines = 1, overflow = TextOverflow.Ellipsis,
                            modifier = Modifier.width(96.dp))
                        Box(Modifier.weight(1f).height(16.dp)) {
                            Canvas(Modifier.matchParentSize()) {
                                val w = (size.width * ((v - lo) / span)).toFloat().coerceAtLeast(2f)
                                drawRoundRect(palette[0], size = Size(w, size.height),
                                    cornerRadius = androidx.compose.ui.geometry.CornerRadius(4f, 4f))
                            }
                        }
                        Text(fmt(v), style = MaterialTheme.typography.labelSmall, modifier = Modifier.padding(start = 6.dp))
                    }
                }
            }
            else -> {
                Canvas(Modifier.fillMaxWidth().height(180.dp)) {
                    val n = chart.labels.size.coerceAtLeast(1)
                    for (g in 0..4) {
                        val y = size.height * g / 4f
                        drawLine(grid, Offset(0f, y), Offset(size.width, y), strokeWidth = 1f)
                    }
                    fun yOf(v: Double) = (size.height * (1 - (v - lo) / span)).toFloat()
                    if (chart.type == "line") {
                        chart.series.forEachIndexed { si, s ->
                            val path = Path()
                            var started = false
                            s.values.forEachIndexed { i, v ->
                                if (v == null) { started = false; return@forEachIndexed }
                                val x = if (n == 1) size.width / 2 else size.width * i / (n - 1f)
                                if (!started) path.moveTo(x, yOf(v)) else path.lineTo(x, yOf(v))
                                started = true
                            }
                            drawPath(path, palette[si % palette.size], style = Stroke(width = 5f, cap = StrokeCap.Round))
                        }
                    } else {
                        val groups = chart.series.size.coerceAtLeast(1)
                        val slot = size.width / n
                        val barW = (slot * 0.8f) / groups
                        chart.series.forEachIndexed { si, s ->
                            s.values.forEachIndexed { i, v ->
                                if (v == null) return@forEachIndexed
                                val x = slot * i + slot * 0.1f + barW * si
                                val top = yOf(max(v, 0.0))
                                val bottom = yOf(min(v, 0.0))
                                drawRect(palette[si % palette.size], Offset(x, top), Size(barW * 0.9f, (bottom - top).coerceAtLeast(2f)))
                            }
                        }
                    }
                }
                Row(Modifier.fillMaxWidth()) {
                    Text(chart.labels.firstOrNull().orEmpty(), style = MaterialTheme.typography.labelSmall, color = ink,
                        modifier = Modifier.weight(1f))
                    Text("${fmt(lo)} – ${fmt(hi)}", style = MaterialTheme.typography.labelSmall, color = ink)
                    Text(chart.labels.lastOrNull().orEmpty(), style = MaterialTheme.typography.labelSmall, color = ink,
                        modifier = Modifier.weight(1f), textAlign = androidx.compose.ui.text.style.TextAlign.End)
                }
            }
        }
        if (chart.series.size > 1) {
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                chart.series.forEachIndexed { si, s ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Box(Modifier.size(10.dp).background(palette[si % palette.size], CircleShape))
                        Spacer(Modifier.width(4.dp))
                        Text(s.name, style = MaterialTheme.typography.labelSmall)
                    }
                }
            }
        }
        TextButton(onClick = { showNumbers = !showNumbers }, contentPadding = androidx.compose.foundation.layout.PaddingValues(0.dp)) {
            Text(if (showNumbers) "Hide the numbers" else "Show the numbers")
        }
        if (showNumbers) {
            TableView(DataTable(
                columns = listOf("") + chart.series.map { it.name },
                rows = chart.labels.mapIndexed { i, label ->
                    listOf(JsonPrimitive(label)) + chart.series.map { s -> JsonPrimitive(s.values.getOrNull(i)) }
                },
            ))
        }
    }
}

private fun cell(e: kotlinx.serialization.json.JsonElement): String {
    val p = e as? JsonPrimitive ?: return e.toString()
    if (p.isString) return p.content
    val raw = p.contentOrNull ?: return ""
    return raw.toDoubleOrNull()?.let(::fmt) ?: raw
}

@Composable
private fun TableView(table: DataTable) {
    val context = LocalContext.current
    Column {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(table.title, style = MaterialTheme.typography.titleSmall, modifier = Modifier.weight(1f))
            IconButton(onClick = {
                val csv = (listOf(table.columns) + table.rows.map { r -> r.map(::cell) })
                    .joinToString("\n") { row -> row.joinToString(",") { "\"" + it.replace("\"", "\"\"") + "\"" } }
                shareText(context, csv)
            }) { Icon(Icons.Outlined.IosShare, contentDescription = "Share as CSV") }
        }
        Column(Modifier.horizontalScroll(rememberScrollState())) {
            Row(Modifier.background(MaterialTheme.colorScheme.surfaceContainer)) {
                table.columns.forEach {
                    Text(it, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.SemiBold,
                        modifier = Modifier.width(120.dp).padding(6.dp))
                }
            }
            table.rows.take(100).forEach { row ->
                HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
                Row {
                    table.columns.indices.forEach { c ->
                        Text(row.getOrNull(c)?.let(::cell).orEmpty(), style = MaterialTheme.typography.bodySmall,
                            modifier = Modifier.width(120.dp).padding(6.dp))
                    }
                }
            }
            if (table.rows.size > 100 || table.more > 0) {
                Text("… ${max(0, table.rows.size - 100) + table.more} more rows (share as CSV to see all)",
                    style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(6.dp))
            }
        }
    }
}
