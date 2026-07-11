package com.hercules.companion

import android.os.Handler
import android.os.Looper
import org.json.JSONObject
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URL
import java.nio.charset.StandardCharsets

/** Fala com o servidor do Hércules. Sem bibliotecas externas: HttpURLConnection + JSONObject
 *  já vêm no Android, o que mantém o app pequeno e o build simples. */
object ApiClient {
    const val BASE_URL = "https://rjuan98.pythonanywhere.com"

    private val mainHandler = Handler(Looper.getMainLooper())

    fun login(email: String, senha: String, callback: (token: String?, nome: String?, erro: String?) -> Unit) {
        Thread {
            try {
                val conn = URL("$BASE_URL/api/token").openConnection() as HttpURLConnection
                conn.requestMethod = "POST"
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                conn.connectTimeout = 15000
                conn.readTimeout = 15000

                val body = JSONObject().put("email", email).put("senha", senha).toString()
                OutputStreamWriter(conn.outputStream, StandardCharsets.UTF_8).use { it.write(body) }

                val code = conn.responseCode
                val stream = if (code in 200..299) conn.inputStream else conn.errorStream
                val respText = stream?.bufferedReader(StandardCharsets.UTF_8)?.use { it.readText() } ?: ""
                val json = if (respText.isNotBlank()) JSONObject(respText) else JSONObject()

                mainHandler.post {
                    if (code in 200..299 && json.optBoolean("ok", false)) {
                        callback(json.optString("token"), json.optString("nome"), null)
                    } else {
                        callback(null, null, json.optString("erro", "Não foi possível entrar (código $code)."))
                    }
                }
            } catch (e: Exception) {
                mainHandler.post { callback(null, null, "Sem conexão com o Hércules: ${e.message}") }
            }
        }.start()
    }

    fun sendCapture(token: String, texto: String, callback: ((Boolean) -> Unit)? = null) {
        Thread {
            var ok = false
            try {
                val conn = URL("$BASE_URL/api/captura").openConnection() as HttpURLConnection
                conn.requestMethod = "POST"
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                conn.connectTimeout = 15000
                conn.readTimeout = 15000

                val body = JSONObject().put("token", token).put("texto", texto).toString()
                OutputStreamWriter(conn.outputStream, StandardCharsets.UTF_8).use { it.write(body) }

                ok = conn.responseCode in 200..299
                conn.inputStream?.close()
            } catch (e: Exception) {
                ok = false
            }
            if (callback != null) mainHandler.post { callback(ok) }
        }.start()
    }
}
