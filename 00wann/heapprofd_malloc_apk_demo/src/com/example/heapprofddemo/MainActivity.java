package com.example.heapprofddemo;

import android.app.Activity;
import android.os.Bundle;
import android.widget.TextView;

public class MainActivity extends Activity {
  static {
    System.loadLibrary("heapprofddemo");
  }

  private static native void nativeStart(String filesDir, long totalBytes,
                                         int startDelaySeconds, int allocSeconds,
                                         int holdSeconds);

  @Override
  protected void onCreate(Bundle savedInstanceState) {
    super.onCreate(savedInstanceState);
    TextView text = new TextView(this);
    text.setText("heapprofd malloc demo running");
    setContentView(text);

    long totalBytes = getIntent().getLongExtra("total_bytes", 1024L * 1024L * 1024L);
    int startDelaySeconds = getIntent().getIntExtra("start_delay_seconds", 10);
    int allocSeconds = getIntent().getIntExtra("alloc_seconds", 60);
    int holdSeconds = getIntent().getIntExtra("hold_seconds", 20);
    nativeStart(getFilesDir().getAbsolutePath(), totalBytes, startDelaySeconds,
        allocSeconds, holdSeconds);
  }
}
