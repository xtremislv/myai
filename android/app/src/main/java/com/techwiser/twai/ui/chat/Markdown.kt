package com.techwiser.twai.ui.chat

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.LinkAnnotation
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextLinkStyles
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.withLink
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import com.techwiser.twai.ui.theme.Mono

/**
 * Renders the assistant's Markdown (headings, lists, quotes, code, tables, links) as Compose
 * nodes, never as HTML: model and web text can't run anything (same rule as md() in app.js).
 */
private sealed interface Block {
    data class Heading(val level: Int, val text: String) : Block
    data class Para(val text: String) : Block
    data class Bullets(val items: List<String>, val ordered: Boolean, val start: Int) : Block
    data class Quote(val text: String) : Block
    data class Code(val text: String) : Block
    data class Table(val header: List<String>, val rows: List<List<String>>) : Block
    data object Rule : Block
}

private val BULLET = Regex("^\\s*[-*+]\\s+(.*)$")
private val ORDERED = Regex("^\\s*(\\d+)[.)]\\s+(.*)$")
private val HEADING = Regex("^(#{1,6})\\s+(.*?)\\s*#*$")
private val RULE = Regex("^\\s*([-*_])(\\s*\\1){2,}\\s*$")
private val TABLE_SEP = Regex("^\\s*\\|?\\s*:?-{2,}:?\\s*(\\|\\s*:?-{2,}:?\\s*)*\\|?\\s*$")

private fun cells(line: String): List<String> =
    line.trim().removePrefix("|").removeSuffix("|").split("|").map { it.trim() }

private fun parse(markdown: String): List<Block> {
    val lines = markdown.replace("\r\n", "\n").split("\n")
    val blocks = mutableListOf<Block>()
    var i = 0
    val para = StringBuilder()
    fun flush() {
        if (para.isNotBlank()) blocks += Block.Para(para.toString().trim())
        para.clear()
    }
    while (i < lines.size) {
        val line = lines[i]
        when {
            line.trimStart().startsWith("```") -> {
                flush()
                val code = StringBuilder()
                i++
                while (i < lines.size && !lines[i].trimStart().startsWith("```")) {
                    code.appendLine(lines[i]); i++
                }
                blocks += Block.Code(code.toString().trimEnd())
            }
            line.isBlank() -> flush()
            HEADING.matches(line) -> {
                flush()
                val m = HEADING.find(line)!!
                blocks += Block.Heading(m.groupValues[1].length, m.groupValues[2])
            }
            RULE.matches(line) -> { flush(); blocks += Block.Rule }
            line.contains('|') && i + 1 < lines.size && TABLE_SEP.matches(lines[i + 1]) -> {
                flush()
                val header = cells(line)
                i += 2
                val rows = mutableListOf<List<String>>()
                while (i < lines.size && lines[i].contains('|') && lines[i].isNotBlank()) {
                    rows += cells(lines[i]); i++
                }
                blocks += Block.Table(header, rows)
                continue
            }
            line.trimStart().startsWith(">") -> {
                flush()
                val quote = StringBuilder()
                while (i < lines.size && lines[i].trimStart().startsWith(">")) {
                    quote.appendLine(lines[i].trimStart().removePrefix(">").trim()); i++
                }
                blocks += Block.Quote(quote.toString().trim())
                continue
            }
            BULLET.matches(line) || ORDERED.matches(line) -> {
                flush()
                val ordered = ORDERED.matches(line)
                val start = if (ordered) ORDERED.find(line)!!.groupValues[1].toIntOrNull() ?: 1 else 1
                val items = mutableListOf<String>()
                while (i < lines.size) {
                    val l = lines[i]
                    val m = (if (ordered) ORDERED else BULLET).find(l)
                    when {
                        m != null && (if (ordered) ORDERED.matches(l) else BULLET.matches(l)) ->
                            items += m.groupValues.last()
                        l.isNotBlank() && l.startsWith("  ") && items.isNotEmpty() ->
                            items[items.lastIndex] = items.last() + "\n" + l.trim()
                        else -> break
                    }
                    i++
                }
                blocks += Block.Bullets(items, ordered, start)
                continue
            }
            else -> para.append(if (para.isEmpty()) line else "\n$line")
        }
        i++
    }
    flush()
    return blocks
}

private val URL = Regex("https?://[^\\s)<>\\]]+")

/** Inline Markdown: **bold**, *italic*, `code`, ~~strike~~, [links](url) and bare URLs. */
fun inline(text: String, linkColor: Color, codeBg: Color): AnnotatedString = buildAnnotatedString {
    var i = 0
    fun link(label: String, url: String) {
        withLink(LinkAnnotation.Url(url, TextLinkStyles(SpanStyle(color = linkColor, textDecoration = TextDecoration.Underline)))) {
            append(label)
        }
    }
    while (i < text.length) {
        val rest = text.substring(i)
        when {
            rest.startsWith("**") || rest.startsWith("__") -> {
                val mark = rest.substring(0, 2)
                val end = text.indexOf(mark, i + 2)
                if (end > i + 2) {
                    withStyle(SpanStyle(fontWeight = FontWeight.SemiBold)) {
                        append(inline(text.substring(i + 2, end), linkColor, codeBg))
                    }
                    i = end + 2
                } else { append(mark); i += 2 }
            }
            rest.startsWith("~~") -> {
                val end = text.indexOf("~~", i + 2)
                if (end > i + 2) {
                    withStyle(SpanStyle(textDecoration = TextDecoration.LineThrough)) { append(text.substring(i + 2, end)) }
                    i = end + 2
                } else { append("~~"); i += 2 }
            }
            rest.startsWith("`") -> {
                val end = text.indexOf('`', i + 1)
                if (end > i) {
                    withStyle(SpanStyle(fontFamily = FontFamily.Monospace, background = codeBg)) {
                        append(text.substring(i + 1, end))
                    }
                    i = end + 1
                } else { append('`'); i++ }
            }
            (rest.startsWith("*") || rest.startsWith("_")) && rest.length > 1 && !rest[1].isWhitespace() -> {
                val mark = rest[0]
                val end = text.indexOf(mark, i + 1)
                val wordInside = mark == '_' && i > 0 && text[i - 1].isLetterOrDigit()
                if (end > i + 1 && !wordInside) {
                    withStyle(SpanStyle(fontStyle = FontStyle.Italic)) { append(inline(text.substring(i + 1, end), linkColor, codeBg)) }
                    i = end + 1
                } else { append(mark); i++ }
            }
            rest.startsWith("[") -> {
                val close = text.indexOf("](", i)
                val endParen = if (close > 0) text.indexOf(')', close) else -1
                val url = if (endParen > 0) text.substring(close + 2, endParen).trim() else ""
                if (close > i && endParen > close && (url.startsWith("http://") || url.startsWith("https://"))) {
                    link(text.substring(i + 1, close), url)
                    i = endParen + 1
                } else { append('['); i++ }
            }
            rest.startsWith("http://") || rest.startsWith("https://") -> {
                val m = URL.find(rest)
                val url = m?.value?.trimEnd('.', ',', ';', ':', '!', '?') ?: rest
                link(url, url)
                i += url.length
            }
            else -> { append(text[i]); i++ }
        }
    }
}

@Composable
fun Markdown(text: String, modifier: Modifier = Modifier, color: Color = MaterialTheme.colorScheme.onSurface) {
    val blocks = remember(text) { parse(text) }
    val link = MaterialTheme.colorScheme.secondary
    val codeBg = MaterialTheme.colorScheme.surfaceContainerHighest
    val body = MaterialTheme.typography.bodyLarge.copy(color = color)
    Column(modifier, verticalArrangement = Arrangement.spacedBy(8.dp)) {
        blocks.forEach { block ->
            when (block) {
                is Block.Heading -> Text(
                    inline(block.text, link, codeBg),
                    style = when (block.level) {
                        1 -> MaterialTheme.typography.titleLarge
                        2 -> MaterialTheme.typography.titleMedium
                        else -> MaterialTheme.typography.titleSmall
                    }.copy(color = color, fontWeight = FontWeight.SemiBold),
                )
                is Block.Para -> Text(inline(block.text, link, codeBg), style = body)
                is Block.Bullets -> Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    block.items.forEachIndexed { n, item ->
                        Row {
                            Text(if (block.ordered) "${block.start + n}." else "•", style = body,
                                modifier = Modifier.widthIn(min = 22.dp))
                            Text(inline(item, link, codeBg), style = body, modifier = Modifier.weight(1f))
                        }
                    }
                }
                is Block.Quote -> Row(Modifier.height(IntrinsicSize.Min)) {
                    Box(Modifier.width(3.dp).fillMaxHeight().background(MaterialTheme.colorScheme.outline))
                    Text(inline(block.text, link, codeBg), style = body.copy(color = MaterialTheme.colorScheme.onSurfaceVariant),
                        modifier = Modifier.padding(start = 10.dp))
                }
                is Block.Code -> Text(
                    block.text,
                    style = Mono.copy(color = color),
                    modifier = Modifier.fillMaxWidth()
                        .background(codeBg, RoundedCornerShape(8.dp))
                        .horizontalScroll(rememberScrollState())
                        .padding(10.dp),
                    softWrap = false,
                )
                is Block.Table -> MarkdownTable(block, color, link, codeBg)
                Block.Rule -> HorizontalDivider()
            }
        }
    }
}

@Composable
private fun MarkdownTable(table: Block.Table, color: Color, link: Color, codeBg: Color) {
    val line = MaterialTheme.colorScheme.outlineVariant
    val cellStyle = MaterialTheme.typography.bodyMedium.copy(color = color)
    Column(
        Modifier.horizontalScroll(rememberScrollState())
            .border(1.dp, line, RoundedCornerShape(8.dp))
    ) {
        val columns = table.header.size
        val widths = (0 until columns).map { c ->
            val longest = (listOf(table.header) + table.rows).maxOf { it.getOrNull(c)?.length ?: 0 }
            (longest * 8 + 24).coerceIn(64, 260).dp
        }
        Row(Modifier.background(MaterialTheme.colorScheme.surfaceContainer)) {
            table.header.forEachIndexed { c, h ->
                Text(inline(h, link, codeBg), style = cellStyle.copy(fontWeight = FontWeight.SemiBold),
                    modifier = Modifier.width(widths[c]).padding(8.dp))
            }
        }
        table.rows.forEach { row ->
            HorizontalDivider(color = line)
            Row(verticalAlignment = Alignment.Top) {
                (0 until columns).forEach { c ->
                    Text(inline(row.getOrElse(c) { "" }, link, codeBg), style = cellStyle,
                        modifier = Modifier.width(widths[c]).padding(8.dp))
                }
            }
        }
    }
}
