package com.chesscoach.mobile

import android.os.Bundle
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.button.MaterialButton
import com.google.android.material.materialswitch.MaterialSwitch
import com.google.android.material.textfield.TextInputEditText
import java.net.HttpURLConnection
import java.net.URL

/**
 * 连接设置：电脑 IP / 端口 / Token。
 *
 * - 「测试连接」用 HttpURLConnection 打 /api/mobile/status，Toast + 状态文字反馈
 * - 「保存」写入 SharedPreferences 后返回 MainActivity
 */
class ConfigActivity : AppCompatActivity() {

    private lateinit var hostInput: TextInputEditText
    private lateinit var portInput: TextInputEditText
    private lateinit var tokenInput: TextInputEditText
    private lateinit var tokenSwitch: MaterialSwitch
    private lateinit var testButton: MaterialButton
    private lateinit var saveButton: MaterialButton
    private lateinit var statusText: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_config)

        hostInput = findViewById(R.id.hostInput)
        portInput = findViewById(R.id.portInput)
        tokenInput = findViewById(R.id.tokenInput)
        tokenSwitch = findViewById(R.id.tokenSwitch)
        testButton = findViewById(R.id.testButton)
        saveButton = findViewById(R.id.saveButton)
        statusText = findViewById(R.id.statusText)

        // 回填已保存的配置（首次为空，用 hint 提示默认值）
        hostInput.setText(Prefs.host(this))
        hostInput.hint = getString(R.string.hint_host)
        portInput.setText(Prefs.port(this).toString())
        tokenInput.setText(Prefs.token(this))
        tokenSwitch.isChecked = Prefs.tokenEnabled(this)
        tokenInput.isEnabled = tokenSwitch.isChecked

        tokenSwitch.setOnCheckedChangeListener { _, checked -> tokenInput.isEnabled = checked }
        testButton.setOnClickListener { testConnection() }
        saveButton.setOnClickListener { saveAndFinish() }
    }

    /** 读取并校验端口；返回 null 表示非法 */
    private fun readPort(): Int? {
        val raw = portInput.text?.toString()?.trim().orEmpty()
        if (raw.isEmpty()) return Prefs.DEFAULT_PORT
        val port = raw.toIntOrNull() ?: return null
        return if (port in 1..65535) port else null
    }

    private fun saveAndFinish() {
        val host = hostInput.text?.toString()?.trim().orEmpty()
        if (host.isEmpty()) {
            Toast.makeText(this, R.string.err_host_empty, Toast.LENGTH_SHORT).show()
            return
        }
        val port = readPort()
        if (port == null) {
            Toast.makeText(this, R.string.err_port_invalid, Toast.LENGTH_SHORT).show()
            return
        }
        Prefs.save(this, host, port, tokenInput.text?.toString().orEmpty(), tokenSwitch.isChecked)
        Toast.makeText(this, R.string.config_saved, Toast.LENGTH_SHORT).show()
        setResult(RESULT_OK)
        finish()
    }

    private fun testConnection() {
        val host = hostInput.text?.toString()?.trim().orEmpty()
        if (host.isEmpty()) {
            Toast.makeText(this, R.string.err_host_empty, Toast.LENGTH_SHORT).show()
            return
        }
        val port = readPort()
        if (port == null) {
            Toast.makeText(this, R.string.err_port_invalid, Toast.LENGTH_SHORT).show()
            return
        }
        val token = if (tokenSwitch.isChecked) tokenInput.text?.toString()?.trim().orEmpty() else ""

        testButton.isEnabled = false
        statusText.setTextColor(getColor(R.color.brand_dark))
        statusText.text = getString(R.string.testing)

        // 简单起见用线程 + runOnUiThread（不引入协程依赖）
        Thread {
            var ok = false
            var message = ""
            try {
                val conn = (URL(Prefs.statusUrl(host, port)).openConnection() as HttpURLConnection).apply {
                    requestMethod = "GET"
                    connectTimeout = 5000
                    readTimeout = 5000
                    if (token.isNotEmpty()) setRequestProperty("X-Mobile-Token", token)
                }
                val code = conn.responseCode
                val stream = if (code in 200..299) conn.inputStream else conn.errorStream
                val body = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
                ok = code in 200..299 && body.contains("\"ok\"")
                message = if (ok) {
                    getString(R.string.test_result_ok, code, body.take(300))
                } else {
                    getString(R.string.test_result_bad, code, body.take(300))
                }
                conn.disconnect()
            } catch (e: Exception) {
                message = getString(R.string.test_failed, e.message ?: e.javaClass.simpleName)
            }

            runOnUiThread {
                testButton.isEnabled = true
                statusText.setTextColor(getColor(if (ok) R.color.ok_green else R.color.err_red))
                statusText.text = message
                Toast.makeText(
                    this,
                    if (ok) R.string.test_result_ok_short else R.string.test_result_bad_short,
                    Toast.LENGTH_SHORT
                ).show()
            }
        }.start()
    }
}
