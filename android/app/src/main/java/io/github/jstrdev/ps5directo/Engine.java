package io.github.jstrdev.ps5directo;

import android.content.Context;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

/**
 * Modo «Usar sin PC»: el propio PS5 Directo (el mismo codigo que en el PC) funcionando dentro del movil,
 * en 127.0.0.1. Lo arranca EngineService (para que Android no lo pare durante los envios) y la ventana
 * espera a que este listo para abrirlo.
 */
final class Engine {
    static volatile String url;
    static volatile String error;
    private static boolean starting;

    private Engine() {
    }

    static synchronized void start(Context ctx) {
        if (url != null || starting) return;
        starting = true;
        error = null;
        final Context app = ctx.getApplicationContext();
        new Thread(() -> {
            try {
                if (!Python.isStarted()) Python.start(new AndroidPlatform(app));
                url = Python.getInstance().getModule("ps5d_boot")
                        .callAttr("start", app.getFilesDir().getAbsolutePath()).toString();
            } catch (Throwable t) {
                error = String.valueOf(t.getMessage());
            } finally {
                synchronized (Engine.class) {
                    starting = false;
                }
            }
        }, "ps5d-engine").start();
    }
}
