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

    /** Busca o token de captura usando o cookie de sessão de quem já está
     *  logado no WebView (login por e-mail/senha feito direto na página real
     *  do Hércules). Sem isso, cobre o caso comum sem nenhuma tela de login
     *  própria do app. */
    fun fetchTokenWithCookie(cookie: String, callback: (token: String?, nome: String?) -> Unit) {
        Thread {
            var result: Pair<String?, String?> = null to null
            try {
                val conn = URL("$BASE_URL/api/meu-token").openConnection() as HttpURLConnection
                conn.requestMethod = "GET"
                conn.setRequestProperty("Cookie", cookie)
                conn.connectTimeout = 15000
                conn.readTimeout = 15000

                if (conn.responseCode in 200..299) {
                    val respText = conn.inputStream.bufferedReader(StandardCharsets.UTF_8).use { it.readText() }
                    val json = JSONObject(respText)
                    if (json.optBoolean("ok", false)) {
                        result = json.optString("token") to json.optString("nome")
                    }
                }
            } catch (e: Exception) {
                // Sem sessão válida ainda, ou sem rede — silencioso, tenta de novo na próxima página
            }
            mainHandler.post { callback(result.first, result.second) }
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
