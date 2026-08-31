package cn.prismloop.mediainjector;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * 启动页:拉起 InjectorService 并持续显示注入状态(诊断用)。
 */
public final class MainActivity extends Activity {

    private final Handler handler = new Handler(Looper.getMainLooper());
    private TextView statusView;

    private final Runnable refresher = new Runnable() {
        @Override
        public void run() {
            InjectorService svc = InjectorServiceHolder.instance;
            if (svc != null) {
                statusView.setText(svc.status());
            }
            handler.postDelayed(this, 1000);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.parseColor("#101014"));
        root.setPadding(48, 96, 48, 48);

        TextView title = new TextView(this);
        title.setText("Prismloop Media Injector");
        title.setTextColor(Color.WHITE);
        title.setTextSize(22);
        root.addView(title);

        statusView = new TextView(this);
        statusView.setTextColor(Color.LTGRAY);
        statusView.setTextSize(12);
        statusView.setPadding(0, 32, 0, 0);
        root.addView(statusView);

        setContentView(root);

        Intent svc = new Intent(this, InjectorService.class);
        startForegroundService(svc);

        handler.post(refresher);
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacks(refresher);
        super.onDestroy();
    }

    /** 服务实例透出(仅同进程内状态读取)。 */
    public static final class InjectorServiceHolder {
        public static volatile InjectorService instance;
    }
}
