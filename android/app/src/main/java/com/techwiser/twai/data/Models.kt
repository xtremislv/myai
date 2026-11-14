package com.techwiser.twai.data

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

// The server's JSON, as in app/api/*.py. Field names follow the API (snake_case).

// ---- auth & devices ----
@Serializable
data class DeviceLoginIn(
    val username: String,
    val password: String,
    val name: String,
    val platform: String = "android",
    val push_token: String? = null,
)

@Serializable
data class DeviceRegisterIn(
    val name: String,
    val platform: String = "android",
    val push_token: String? = null,
    val device_id: String? = null,
)

@Serializable
data class RefreshIn(val device_id: String, val refresh_token: String)

@Serializable
data class DeviceTokenOut(
    val access_token: String,
    val expires_in: Int = 3600,
    val refresh_token: String,
    val device_id: String,
    val must_change_password: Boolean = false,
)

@Serializable
data class TokenOut(
    val access_token: String,
    val must_change_password: Boolean = false,
    val recovery_code: String? = null,
)

@Serializable
data class ChangePasswordIn(val old_password: String, val new_password: String)

@Serializable
data class RecoverIn(val username: String, val recovery_code: String, val new_password: String)

@Serializable
data class PasswordIn(val password: String)

@Serializable
data class RecoveryCodeOut(val recovery_code: String)

@Serializable
data class Profile(
    val id: String,
    val username: String,
    val is_admin: Boolean = false,
    val departments: List<String> = emptyList(),
    val chat_enabled: Boolean = false,
    val created_at: String = "",
)

@Serializable
data class DeviceOut(
    val id: String,
    val name: String,
    val platform: String = "android",
    val push: Boolean = false,
    val created_at: String = "",
    val last_used_at: String? = null,
)

@Serializable
data class PushIn(val push_token: String?)

@Serializable
data class AppConfig(
    val api_version: Int = 1,
    val push_enabled: Boolean = false,
    val token_ttl_seconds: Int = 3600,
    val poll_minutes: Int = 15,
    val attachment_max_bytes: Long = 10L * 1024 * 1024,
    val android: AndroidRelease? = null,
)

/** The app the server publishes (app/app_release.py), for self-update. */
@Serializable
data class AndroidRelease(
    val version_code: Int,
    val version_name: String = "",
    val sha256: String,
    val size: Long = 0,
    val notes: String = "",
    val min_version_code: Int = 0,
    val published_at: String = "",
    val url: String,
)

@Serializable
data class Prefs(
    val timezone: String = "",
    val email: String = "",
    val digest_hour: Int? = null,
    val digest_email: Boolean = false,
    val email_reminders: Boolean = false,
)

// ---- chat ----
@Serializable
data class ChatIn(val message: String, val thread_id: String, val mode: String = "auto")

@Serializable
data class ConfirmOut(
    val tool: String,
    val title: String,
    val details: Map<String, String> = emptyMap(),
    val purchase: Boolean = false,
)

@Serializable
data class ConfirmIn(val approve: Boolean, val as_work: Boolean = false)

@Serializable
data class SecretAsk(val ref: Int, val what: String = "", val site: String = "")

@Serializable
data class BrowserHandover(val scope: String = "private", val reason: String = "", val secret: SecretAsk? = null)

@Serializable
data class ChatOut(
    val thread_id: String = "",
    val reply: String = "",
    val mode: String = "",
    val outcome: String = "ok",
    val reason: String = "",
    val route: String = "private",
    val department: String? = null,
    val approval_id: String? = null,
    val task_id: String? = null,
    val confirm: ConfirmOut? = null,
    val browser: BrowserHandover? = null,
    val dashboards: List<JsonObject> = emptyList(),
)

@Serializable
data class ThreadOut(val thread_id: String, val title: String, val updated_at: String)

@Serializable
data class MessageOut(
    val role: String,
    val content: String,
    val handled_by: String? = null,
    val dashboards: List<JsonObject> = emptyList(),
)

@Serializable
data class RunningOut(
    val id: String,
    val kind: String = "message",
    val message: String = "",
    val steps: List<String> = emptyList(),
    val seconds: Int = 0,
)

@Serializable
data class RunStatusOut(val state: String)

@Serializable
data class ChatRequestOut(
    val id: String,
    val kind: String,
    val department: String,
    val label: String,
    val state: String,
    val result: String? = null,
)

@Serializable
data class AttachmentOut(
    val id: String,
    val thread_id: String,
    val name: String,
    val content_type: String = "",
    val size: Long = 0,
    val created_at: String = "",
)

@Serializable
data class TranscriptOut(val text: String = "", val language: String = "", val seconds: Double = 0.0)

@Serializable
data class UploadOut(val outcome: String = "ok", val reason: String = "", val attachment: AttachmentOut? = null)

// ---- notifications & reminders ----
@Serializable
data class NotificationOut(
    val id: String,
    val kind: String,
    val title: String,
    val body: String = "",
    val read: Boolean = false,
    val created_at: String = "",
)

@Serializable
data class CountOut(val unread: Int)

@Serializable
data class ReadIn(val id: String? = null)

@Serializable
data class ReminderIn(val `when`: String, val text: String, val repeat: String = "none", val email: Boolean = false)

@Serializable
data class ReminderOut(
    val id: String,
    val text: String,
    val due_at: String = "",
    val due: String = "",
    val repeat: String = "none",
    val email: Boolean = false,
    val source: String = "",
)

// ---- files ----
@Serializable
data class FileOut(
    val id: String,
    val path: String,
    val size: Long = 0,
    val content_type: String = "",
    val version: Int = 1,
    val versions: Int = 1,
    val updated_at: String = "",
    val deleted_at: String? = null,
)

@Serializable
data class FilesOut(val files: List<FileOut> = emptyList(), val used: Long = 0, val quota: Long = 0)

// ---- work: approvals, inbox, reports ----
@Serializable
data class ApprovalOut(
    val id: String,
    val department: String,
    val subject_id: String = "",
    val subject_name: String = "",
    val message: String = "",
    val thread_id: String = "",
    val mode: String = "",
    val state: String = "pending",
    val tool: String,
    val tool_args: JsonObject = JsonObject(emptyMap()),
)

@Serializable
data class DecisionIn(val reason: String = "")

@Serializable
data class RequestOut(
    val outcome: String,
    val department: String? = null,
    val reply: String? = null,
    val approval_id: String? = null,
    val task_id: String? = null,
    val report_id: String? = null,
    val reason: String = "",
)

@Serializable
data class TaskOut(
    val id: String,
    val department: String,
    val requester_id: String = "",
    val requester_name: String = "",
    val kind: String = "",
    val message: String = "",
    val state: String = "",
)

@Serializable
data class ReportOut(
    val id: String,
    val kind: String,
    val subject_id: String = "",
    val subject_name: String = "",
    val message: String = "",
    val state: String = "",
)

// ---- dashboards (app/dashboards.py) ----
@Serializable
data class Kpi(val label: String = "", val value: String = "", val change: String = "", val trend: String = "")

@Serializable
data class Series(val name: String = "", val values: List<Double?> = emptyList())

@Serializable
data class Chart(
    val type: String = "bar",
    val title: String = "",
    val labels: List<String> = emptyList(),
    val series: List<Series> = emptyList(),
    val unit: String = "",
)

@Serializable
data class DataTable(
    val title: String = "",
    val columns: List<String> = emptyList(),
    val rows: List<List<JsonElement>> = emptyList(),
    val more: Int = 0,
)

@Serializable
data class Dashboard(
    val id: String = "",
    val title: String = "Dashboard",
    val subtitle: String = "",
    val kpis: List<Kpi> = emptyList(),
    val charts: List<Chart> = emptyList(),
    val tables: List<DataTable> = emptyList(),
    val notes: String = "",
)

/** One server-sent event from /v1/chat/stream or .../confirm/stream. */
sealed interface StreamEvent {
    data class Run(val id: String) : StreamEvent
    data class Step(val text: String) : StreamEvent
    data class Delta(val text: String) : StreamEvent
    data class Done(val out: ChatOut) : StreamEvent
    data class Failed(val detail: String) : StreamEvent
}
