c=open("static/index.html").read()
c=c.replace("initThree();","const mV={u:0,d:0,l:0,r:0};\ninitThree();")
open("static/index.html","w").write(c)
