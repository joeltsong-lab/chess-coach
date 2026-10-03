package com.chesscoach.mobile

import android.content.Context

/**
 * 电脑地址 / 端口 / Token 的本地存储（SharedPreferences）。
 * 首次启动时由 ConfigActivity 写入，之后 MainActivity 直接读取。
 */
object Prefs {

    private const val FILE = "chess_coach_prefs"

    private const val KEY_HOST = "host"
    private const val KEY_PORT = "port"
    private const val KEY_TOKEN = "token"
    private const val KEY_TOKEN_ENABLED = "token_enabled"

    /** 默认端口与 scripts/run.sh 里的 mobile 实例一致 */
    const val DEFAULT_PORT = 5002

    private fun sp(ctx: Context) =
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun host(ctx: Context): String =
        sp(ctx).getString(KEY_HOST, "").orEmpty().trim()

    fun port(ctx: Context): Int = sp(ctx).getInt(KEY_PORT, DEFAULT_PORT)

    fun token(ctx: Context): String =
        sp(ctx).getString(KEY_TOKEN, "").orEmpty().trim()

    fun tokenEnabled(ctx: Context): Boolean =
        sp(ctx).getBoolean(KEY_TOKEN_ENABLED, false)

    /** 是否已经配置过电脑地址 */
    fun isConfigured(ctx: Context): Boolean = host(ctx).isNotEmpty()

    /** 实际要发的 Token：开关关掉时返回空串 */
    fun effectiveToken(ctx: Context): String =
        if (tokenEnabled(ctx)) token(ctx) else ""

    fun save(ctx: Context, host: String, port: Int, token: String, tokenEnabled: Boolean) {
        sp(ctx).edit()
            .putString(KEY_HOST, host.trim())
            .putInt(KEY_PORT, port)
            .putString(KEY_TOKEN, token.trim())
            .putBoolean(KEY_TOKEN_ENABLED, tokenEnabled)
            .apply()
    }

    /** 手机版页面地址，如 http://192.168.1.100:5002/mobile */
    fun pageUrl(ctx: Context): String = "http://${host(ctx)}:${port(ctx)}/mobile"

    /** 状态接口地址，「测试连接」用 */
    fun statusUrl(host: String, port: Int): String =
        "http://$host:$port/api/mobile/status"
}
