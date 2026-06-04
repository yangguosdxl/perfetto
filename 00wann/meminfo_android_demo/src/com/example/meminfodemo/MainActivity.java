package com.example.meminfodemo;

import android.app.Activity;
import android.content.ContentValues;
import android.database.sqlite.SQLiteDatabase;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.media.ImageReader;
import android.opengl.GLES20;
import android.opengl.GLSurfaceView;
import android.os.Bundle;
import android.util.Log;
import android.view.Surface;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.File;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.List;

import javax.microedition.khronos.egl.EGLConfig;
import javax.microedition.khronos.opengles.GL10;

public class MainActivity extends Activity {
  private static final String TAG = "MeminfoDemo";
  private static final int JAVA_HEAP_MB = 48;
  private static final int NATIVE_MALLOC_MB = 64;
  private static final int ANON_MMAP_MB = 64;
  private static final int FILE_MMAP_MB = 32;
  private static final int SQLITE_BLOB_ROWS = 2048;
  private static final int SQLITE_STATEMENTS = 256;
  private static final int IMAGE_READER_COUNT = 4;
  private static final int IMAGE_READER_SIZE = 2048;

  static {
    System.loadLibrary("meminfodemo");
  }

  private final List<byte[]> javaBlocks = new ArrayList<byte[]>();
  private final List<android.database.sqlite.SQLiteStatement> sqliteStatements =
      new ArrayList<android.database.sqlite.SQLiteStatement>();
  private final List<ImageReader> imageReaders = new ArrayList<ImageReader>();
  private TextView statusView;
  private DemoGlView glView;
  private SQLiteDatabase database;

  private static native long nativeAllocateMalloc(int mb);
  private static native long nativeAllocateAnonMmap(int mb);
  private static native long nativeAllocateFileMmap(String path, int mb);
  private static native void nativeReleaseAll();

  @Override
  protected void onCreate(Bundle state) {
    super.onCreate(state);
    buildUi();
    if (getIntent().getBooleanExtra("auto_allocate", false)) {
      statusView.postDelayed(new Runnable() {
        @Override
        public void run() {
          allocateAll();
        }
      }, 800);
    }
  }

  private void buildUi() {
    LinearLayout root = new LinearLayout(this);
    root.setOrientation(LinearLayout.VERTICAL);
    root.setPadding(24, 24, 24, 24);

    Button allocate = new Button(this);
    allocate.setText("分配 meminfo demo 内存");
    allocate.setOnClickListener(new android.view.View.OnClickListener() {
      @Override
      public void onClick(android.view.View view) {
        allocateAll();
      }
    });
    root.addView(allocate, new LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

    Button release = new Button(this);
    release.setText("释放 demo 内存");
    release.setOnClickListener(new android.view.View.OnClickListener() {
      @Override
      public void onClick(android.view.View view) {
        releaseAll();
      }
    });
    root.addView(release, new LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

    glView = new DemoGlView(this);
    root.addView(glView, new LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT, 320));

    statusView = new TextView(this);
    statusView.setTextSize(14);
    statusView.setText("等待分配\n");
    ScrollView scroll = new ScrollView(this);
    scroll.addView(statusView);
    root.addView(scroll, new LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));

    setContentView(root);
  }

  private void allocateAll() {
    append("开始分配 demo 内存");
    new Thread(new Runnable() {
      @Override
      public void run() {
        try {
          allocateJavaHeap();
          long mallocBytes = nativeAllocateMalloc(NATIVE_MALLOC_MB);
          append("native malloc 已触页: " + mallocBytes / 1024 + " KB");

          long anonBytes = nativeAllocateAnonMmap(ANON_MMAP_MB);
          append("匿名 mmap 已触页: " + anonBytes / 1024 + " KB");

          File mmapFile = new File(getFilesDir(), "demo_other_mmap.bin");
          long fileBytes = nativeAllocateFileMmap(mmapFile.getAbsolutePath(), FILE_MMAP_MB);
          append("文件 mmap 已触页: " + fileBytes / 1024 + " KB path=" + mmapFile);

          allocateSqlite();
          allocateViews();
          allocateGraphicBuffers();
          glView.allocateTextures();
          append("已请求图形缓冲和 GL texture 分配，等待 dumpsys meminfo 观察 Graphics/GL mtrack");
          Log.i(TAG, "MEMINFO_DEMO_READY");
        } catch (Throwable t) {
          Log.e(TAG, "demo 分配失败", t);
          append("ERROR: " + t);
        }
      }
    }, "meminfo-demo-allocator").start();
  }

  private void allocateJavaHeap() {
    // 逐页写入，避免只保留虚拟地址空间而没有真实驻留页。
    for (int i = 0; i < JAVA_HEAP_MB; i++) {
      byte[] block = new byte[1024 * 1024];
      for (int j = 0; j < block.length; j += 4096) {
        block[j] = (byte) i;
      }
      javaBlocks.add(block);
    }
    append("Java heap 已触页: " + JAVA_HEAP_MB + " MB");
  }

  private void allocateSqlite() {
    // 保持数据库连接和 SQLiteStatement，便于 dumpsys SQL/DATABASES 看到稳定统计。
    database = SQLiteDatabase.openOrCreateDatabase(
        new File(getFilesDir(), "demo.db").getAbsolutePath(), null);
    database.execSQL("CREATE TABLE IF NOT EXISTS blobs (id INTEGER PRIMARY KEY, payload BLOB)");
    database.beginTransaction();
    byte[] payload = new byte[4096];
    for (int i = 0; i < payload.length; i++) {
      payload[i] = (byte) (i & 0x7f);
    }
    try {
      for (int i = 0; i < SQLITE_BLOB_ROWS; i++) {
        ContentValues values = new ContentValues();
        values.put("payload", payload);
        database.insert("blobs", null, values);
      }
      database.setTransactionSuccessful();
    } finally {
      database.endTransaction();
    }
    for (int i = 0; i < SQLITE_STATEMENTS; i++) {
      sqliteStatements.add(database.compileStatement(
          "SELECT COUNT(*) FROM blobs WHERE id >= " + i));
    }
    append("SQLite 已写入并保持 statement: rows=" + SQLITE_BLOB_ROWS
        + " statements=" + SQLITE_STATEMENTS);
  }

  private void allocateViews() {
    runOnUiThread(new Runnable() {
      @Override
      public void run() {
        ViewGroup root = (ViewGroup) statusView.getParent().getParent();
        for (int i = 0; i < 80; i++) {
          TextView view = new TextView(MainActivity.this);
          view.setText("meminfo view marker " + i);
          root.addView(view, 2);
        }
        append("View 对象已增加: 80");
      }
    });
  }

  private void allocateGraphicBuffers() {
    // 图形缓冲归因依赖设备 memtrack HAL；demo 实际分配，但 dumpsys 可能只给 WARN。
    Paint paint = new Paint();
    paint.setColor(0xff3366cc);
    for (int i = 0; i < IMAGE_READER_COUNT; i++) {
      ImageReader reader = ImageReader.newInstance(
          IMAGE_READER_SIZE, IMAGE_READER_SIZE, PixelFormat.RGBA_8888, 3);
      imageReaders.add(reader);
      Surface surface = reader.getSurface();
      Canvas canvas = surface.lockCanvas(null);
      try {
        canvas.drawColor(0xff101010 + i * 0x00080808);
        canvas.drawRect(0, 0, IMAGE_READER_SIZE, IMAGE_READER_SIZE, paint);
      } finally {
        surface.unlockCanvasAndPost(canvas);
      }
    }
    append("ImageReader 图形缓冲已创建: count=" + IMAGE_READER_COUNT
        + " size=" + IMAGE_READER_SIZE + "x" + IMAGE_READER_SIZE);
  }

  private void releaseAll() {
    javaBlocks.clear();
    for (android.database.sqlite.SQLiteStatement statement : sqliteStatements) {
      statement.close();
    }
    sqliteStatements.clear();
    if (database != null) {
      database.close();
      database = null;
    }
    for (ImageReader reader : imageReaders) {
      reader.close();
    }
    imageReaders.clear();
    nativeReleaseAll();
    glView.releaseTextures();
    append("已释放 demo 持有对象，可再次 dumpsys 对比");
  }

  private void append(final String line) {
    Log.i(TAG, line);
    runOnUiThread(new Runnable() {
      @Override
      public void run() {
        statusView.append(line + "\n");
      }
    });
  }

  private static final class DemoGlView extends GLSurfaceView {
    private final DemoRenderer renderer = new DemoRenderer();

    DemoGlView(Activity activity) {
      super(activity);
      setEGLContextClientVersion(2);
      setRenderer(renderer);
      setRenderMode(GLSurfaceView.RENDERMODE_WHEN_DIRTY);
    }

    void allocateTextures() {
      queueEvent(new Runnable() {
        @Override
        public void run() {
          renderer.allocateTextures();
        }
      });
      requestRender();
    }

    void releaseTextures() {
      queueEvent(new Runnable() {
        @Override
        public void run() {
          renderer.releaseTextures();
        }
      });
      requestRender();
    }
  }

  private static final class DemoRenderer implements GLSurfaceView.Renderer {
    private static final int TEXTURE_COUNT = 4;
    private static final int TEXTURE_SIZE = 2048;
    private final int[] textures = new int[TEXTURE_COUNT];
    private boolean allocated;

    @Override
    public void onSurfaceCreated(GL10 gl, EGLConfig config) {
      GLES20.glClearColor(0.1f, 0.1f, 0.1f, 1.0f);
    }

    @Override
    public void onSurfaceChanged(GL10 gl, int width, int height) {
      GLES20.glViewport(0, 0, width, height);
    }

    @Override
    public void onDrawFrame(GL10 gl) {
      GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT);
    }

    void allocateTextures() {
      if (allocated) {
        return;
      }
      GLES20.glGenTextures(TEXTURE_COUNT, textures, 0);
      ByteBuffer pixels = ByteBuffer.allocateDirect(TEXTURE_SIZE * TEXTURE_SIZE * 4);
      for (int offset = 0; offset < pixels.capacity(); offset += 4096) {
        pixels.put(offset, (byte) 0x7f);
      }
      for (int texture : textures) {
        pixels.position(0);
        GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, texture);
        GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_MIN_FILTER,
            GLES20.GL_LINEAR);
        GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_MAG_FILTER,
            GLES20.GL_LINEAR);
        GLES20.glTexImage2D(GLES20.GL_TEXTURE_2D, 0, GLES20.GL_RGBA, TEXTURE_SIZE,
            TEXTURE_SIZE, 0, GLES20.GL_RGBA, GLES20.GL_UNSIGNED_BYTE,
            pixels);
      }
      allocated = true;
      Log.i(TAG, "GL textures 已分配: count=" + TEXTURE_COUNT
          + " size=" + TEXTURE_SIZE + "x" + TEXTURE_SIZE);
    }

    void releaseTextures() {
      if (!allocated) {
        return;
      }
      GLES20.glDeleteTextures(TEXTURE_COUNT, textures, 0);
      allocated = false;
      Log.i(TAG, "GL textures 已释放");
    }
  }
}
