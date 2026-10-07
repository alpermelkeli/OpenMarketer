import subprocess, sys, re, pathlib
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
root=pathlib.Path(__file__).parent
names=sys.argv[1:] or [p.stem for p in sorted((root/'html').glob('fig_*.html'))]
for n in names:
    url=f"file://{root/'html'/(n+'.html')}"
    base=[CHROME,"--headless=new","--disable-gpu","--hide-scrollbars","--allow-file-access-from-files","--no-sandbox"]
    dom=subprocess.run(base+["--virtual-time-budget=3000","--window-size=2400,3000","--dump-dom",url],capture_output=True,text=True).stdout
    m=re.search(r'data-w="(\d+)"[^>]*data-h="(\d+)"|data-h="(\d+)"[^>]*data-w="(\d+)"',dom)
    if not m: print(n,"FAILED to measure"); continue
    w,h=(int(m.group(1)),int(m.group(2))) if m.group(1) else (int(m.group(4)),int(m.group(3)))
    out=root/'figures'/(n+'.png')
    subprocess.run(base+["--force-device-scale-factor=2","--virtual-time-budget=3000",f"--window-size={w},{h}",f"--screenshot={out}",url],capture_output=True)
    print(n,w,h,out.exists())
