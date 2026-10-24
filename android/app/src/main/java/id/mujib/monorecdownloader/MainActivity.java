package id.mujib.monorecdownloader;

import android.app.Activity;
import android.os.Bundle;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.view.Gravity;
import android.view.ViewGroup;
import android.widget.*;
import android.text.InputType;

public class MainActivity extends Activity {
  int bg=0xff0e0f10, panel=0xff1b1c1e, line=0xff393b3e, white=0xfff4f4f4, muted=0xff999b9e;
  EditText url; Spinner quality, format; TextView status;
  int dp(float n){return (int)(n*getResources().getDisplayMetrics().density+.5f);}
  GradientDrawable shape(int color){GradientDrawable d=new GradientDrawable();d.setColor(color);d.setCornerRadius(dp(13));d.setStroke(dp(1),line);return d;}
  LinearLayout.LayoutParams lp(int w,int h){return new LinearLayout.LayoutParams(w<0?w:dp(w),h<0?h:dp(h));}
  void gap(LinearLayout p,int h){p.addView(new android.view.View(this),lp(1,h));}
  TextView text(String s,int size,int color,boolean bold){TextView t=new TextView(this);t.setText(s);t.setTextSize(size);t.setTextColor(color);if(bold)t.setTypeface(Typeface.DEFAULT,Typeface.BOLD);return t;}
  void label(LinearLayout p,String s){TextView t=text(s,10,muted,true);t.setLetterSpacing(.12f);p.addView(t);gap(p,7);}
  Spinner spin(String[] items){Spinner s=new Spinner(this);ArrayAdapter<String>a=new ArrayAdapter<String>(this,android.R.layout.simple_spinner_item,items);a.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);s.setAdapter(a);s.setBackground(shape(panel));return s;}
  @Override public void onCreate(Bundle b){
    super.onCreate(b);getWindow().setStatusBarColor(bg);getWindow().setNavigationBarColor(bg);
    LinearLayout page=new LinearLayout(this);page.setOrientation(1);page.setPadding(dp(22),dp(28),dp(22),dp(20));page.setBackgroundColor(bg);
    ScrollView sc=new ScrollView(this);LinearLayout c=new LinearLayout(this);c.setOrientation(1);sc.addView(c);page.addView(sc,new LinearLayout.LayoutParams(-1,0,1));
    TextView title=text("↓   Downloader",22,white,true);c.addView(title);TextView sub=text("MONOREC / PERSONAL MEDIA TOOL",10,muted,false);c.addView(sub);gap(c,30);
    label(c,"VIDEO URL");url=new EditText(this);url.setHint("Tempel tautan video di sini");url.setHintTextColor(0xff727477);url.setTextColor(white);url.setTextSize(14);url.setInputType(InputType.TYPE_CLASS_TEXT|InputType.TYPE_TEXT_VARIATION_URI);url.setPadding(dp(14),dp(12),dp(14),dp(12));url.setBackground(shape(panel));c.addView(url,lp(-1,90));gap(c,20);
    label(c,"FORMAT");format=spin(new String[]{"Video MP4","Audio MP3"});c.addView(format,lp(-1,52));gap(c,18);
    label(c,"KUALITAS VIDEO");quality=spin(new String[]{"1080p FHD","1440p QHD","2160p 4K","720p HD","480p"});c.addView(quality,lp(-1,52));gap(c,20);
    TextView note=text("File disimpan ke folder Download. Proses berjalan lewat Termux.",12,muted,false);note.setPadding(dp(12),dp(12),dp(12),dp(12));note.setBackground(shape(panel));c.addView(note,lp(-1,-2));gap(c,22);
    Button go=new Button(this);go.setText("MULAI DOWNLOAD");go.setTextColor(bg);go.setTypeface(Typeface.DEFAULT,Typeface.BOLD);go.setBackgroundColor(white);c.addView(go,lp(-1,54));status=text("READY  /  Menunggu tautan",12,muted,false);status.setPadding(0,dp(17),0,0);c.addView(status);
    TextView foot=text("MONOREC  •  LOCAL DOWNLOAD ENGINE",10,0xff5a5c5f,false);foot.setGravity(Gravity.CENTER);page.addView(foot,lp(-1,30));setContentView(page);
    if(Intent.ACTION_SEND.equals(getIntent().getAction()) && "text/plain".equals(getIntent().getType())){CharSequence x=getIntent().getCharSequenceExtra(Intent.EXTRA_TEXT);if(x!=null)url.setText(x.toString().trim());}
    go.setOnClickListener(v->download());
  }
  void download(){
    String u=url.getText().toString().trim();
    if(!(u.startsWith("https://")||u.startsWith("http://"))){url.setError("Masukkan URL yang valid");return;}
    String[] heights={"1080","1440","2160","720","480"};
    String q=heights[quality.getSelectedItemPosition()];
    String quoted="'"+u.replace("'","'\\''")+"'";
    String out="$HOME/storage/shared/Download/%(title).150B [%(id)s].%(ext)s";
    String task;
    if(format.getSelectedItemPosition()==1) task="yt-dlp --no-playlist -f 'ba/b' --extract-audio --audio-format mp3 -o \""+out+"\" "+quoted;
    else task="yt-dlp --no-playlist -f 'bv*[height<="+q+"]+ba/b[height<="+q+"]/b' --merge-output-format mp4 -o \""+out+"\" "+quoted;
    String cmd="termux-notification --id 741 --title 'MonoRec Downloader' --content 'Download dimulai' >/dev/null 2>&1; "+task+"; rc=$?; if [ $rc -eq 0 ]; then termux-notification --id 741 --title 'MonoRec Downloader' --content 'Download selesai. Cek folder Download.' >/dev/null 2>&1; else termux-notification --id 741 --title 'MonoRec Downloader' --content 'Download gagal. Periksa Termux.' >/dev/null 2>&1; fi; exit $rc;";
    Intent i=new Intent("com.termux.RUN_COMMAND");i.setClassName("com.termux","com.termux.app.RunCommandService");
    i.putExtra("com.termux.RUN_COMMAND_PATH","/data/data/com.termux/files/usr/bin/bash");i.putExtra("com.termux.RUN_COMMAND_ARGUMENTS",new String[]{"-lc",cmd});i.putExtra("com.termux.RUN_COMMAND_WORKDIR","/data/data/com.termux/files/home");i.putExtra("com.termux.RUN_COMMAND_BACKGROUND",true);
    try{startService(i);status.setText("SENT  /  Perintah dikirim ke Termux");Toast.makeText(this,"Download dikirim. Notifikasi mulai/selesai memakai Termux:API.",Toast.LENGTH_LONG).show();}
    catch(Exception e){new android.app.AlertDialog.Builder(this).setTitle("Aktifkan integrasi Termux").setMessage("Di Termux jalankan: mkdir -p ~/.termux && echo 'allow-external-apps = true' >> ~/.termux/termux.properties && termux-reload-settings").setPositiveButton("OK",null).show();}
  }
}