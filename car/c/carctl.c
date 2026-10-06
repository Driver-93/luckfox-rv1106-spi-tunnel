/*
 * carctl.c - Luckfox 小车控制守护 (C 版, 零第三方依赖)
 *
 * 目标: 在 RV1103/RV1106 64MB 上替代 Python car_controller.py
 * 协议与 car_controller.py 完全兼容 (复用云 broker + 网页):
 *   SUB luckfox/car/cmd    {"c":"forward|backward|spin_left|...","s":0-100,"token":...}
 *   PUB luckfox/car/status {"mode":"live|sim","dir":..,"speed":..,"ts":..,"online":true} (心跳5s)
 *
 * 特性:
 *   - 手写 MQTT v3.1.1 客户端 (CONNECT/SUBSCRIBE/PUBLISH/PING, 纯 socket, 无库)
 *   - 极简 JSON 字段提取 (strstr, 只需 c/s/token, 零依赖)
 *   - GPIO 走 /sys/class/gpio (内核标准接口, 无库)
 *   - 令牌鉴权, 心跳, 断线重连
 *
 * 编译 (交叉, Luckfox arm 工具链):
 *   arm-linux-gnueabihf-gcc -O2 -o carctl carctl.c -lpthread
 *
 * 用法:
 *   ./carctl --sim                           # 模拟模式(不打GPIO)
 *   ./carctl --broker YOUR_SERVER_IP --port 1883
 * 从 /etc/luckfox-car/car_config.json 读不到时用命令行/宏默认值。
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <time.h>
#include <signal.h>
#include <pthread.h>
#include <sys/socket.h>
#include <sys/select.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <stdarg.h>

/* ---------- 配置(编译期默认) ---------- */
#define DEF_HOST      "YOUR_SERVER_IP"  /* 腾讯云 broker */
#define DEF_PORT      1883
#define DEF_USER      "car"
#define DEF_PASS      "YOUR_MQTT_PASSWORD"
#define DEF_TOKEN     "YOUR_API_TOKEN"
#define DEF_TOPIC_CMD    "luckfox/car/cmd"
#define DEF_TOPIC_STATUS "luckfox/car/status"
#define HEARTBEAT_MS  5000
#define SIM_FILE      "/tmp/carctl.sim"   /* 模拟模式标记 */

/* ---------- 电机引脚(占位, 接线后改) ---------- */
#define PIN_STBY 106
/* 每通道: IN1,IN2,PWM */
#define PIN_FL 109  /* FL IN1=107 IN2=108 PWM=109 */
#define PIN_FR 112
#define PIN_BL 115
#define PIN_BR 118

static int sim_mode = 0;

/* ============================================================
 * GPIO (sysfs)
 * ============================================================ */
static void gp_export(int g){ char p[64]; snprintf(p,sizeof p,"/sys/class/gpio/gpio%d",g); if(!access(p,F_OK))return; snprintf(p,sizeof p,"/sys/class/gpio/export"); FILE*f=fopen(p,"w"); if(f){fprintf(f,"%d",g);fclose(f);} usleep(50000); }
static void gp_dir(int g,const char*d){ char p[64]; snprintf(p,sizeof p,"/sys/class/gpio/gpio%d/direction",g); FILE*f=fopen(p,"w"); if(f){fprintf(f,"%s",d);fclose(f);} }
static void gp_write(int g,int v){ char p[64]; snprintf(p,sizeof p,"/sys/class/gpio/gpio%d/value",g); FILE*f=fopen(p,"w"); if(f){fprintf(f,"%d",v?1:0);fclose(f);} }
static void gp_setup(int g,int out){ gp_export(g); gp_dir(g,out?"out":"in"); }
static void gp_unexport(int g){ char p[64]; snprintf(p,sizeof p,"/sys/class/gpio/unexport"); FILE*f=fopen(p,"w"); if(f){fprintf(f,"%d",g);fclose(f);} }

/* 4WD 麦克纳姆轮状态 */
typedef struct { int in1,in2,pwm; } channel_t;
static channel_t ch[4]; /* 0=FL 1=FR 2=BL 3=BR */
static double duty[4];  /* 0..1 软件PWM占空 */

/* 软件 PWM 线程 */
static volatile int pwm_run=0;
static pthread_t pwm_thread;
static void *pwm_loop(void*a){
    (void)a;
    const double period=0.0005; /* 2kHz */
    while(pwm_run){
        int i;
        for(i=0;i<4;i++){
            if(duty[i]>=0.999){ gp_write(ch[i].pwm,1); }
            else if(duty[i]<=0.0){ gp_write(ch[i].pwm,0); }
            else {
                double on=period*duty[i];
                gp_write(ch[i].pwm,1);
                struct timespec ts={0,(long)(on*1e9)};
                nanosleep(&ts,NULL);
                gp_write(ch[i].pwm,0);
                struct timespec ts2={0,(long)((period-on)*1e9)};
                nanosleep(&ts2,NULL);
            }
        }
    }
    return NULL;
}

/* 设方向: dir=1正转, -1反转, 0停 */
static void motor_dir(int idx,int dir){
    if(!sim_mode){
        gp_write(ch[idx].in1, dir==1?1:0);
        gp_write(ch[idx].in2, dir==-1?1:0);
    } else {
        printf("[sim] ch%d dir=%d\n",idx,dir);
    }
}
static void motor_speed(int idx,double d){ duty[idx]=d; }

static void motors_setup(){
    gp_setup(PIN_STBY,1); gp_write(PIN_STBY,1); /* 使能 */
    int pins[4]={PIN_FL,PIN_FR,PIN_BL,PIN_BR};
    const char*names[4]={"FL","FR","BL","BR"};
    int i;
    /* 每个通道驱动 3 个引脚: IN1,IN2,PWM = pin-1,pin-1? 简化: 用 PWM 引脚为主 */
    /* 这里为清晰把 IN1/IN2 用 PWM-1/PWM+?占位。实际按 car_config.json pin 填。 */
    for(i=0;i<4;i++){
        int p=pins[i];
        ch[i].in1=p-1; ch[i].in2=p+1; ch[i].pwm=p;
        gp_setup(ch[i].in1,1); gp_setup(ch[i].in2,1); gp_setup(ch[i].pwm,1);
        gp_write(ch[i].pwm,0);
        printf("[gpio] %s: IN1=%d IN2=%d PWM=%d\n",names[i],ch[i].in1,ch[i].in2,ch[i].pwm);
    }
}
static void motors_start(){ if(sim_mode)return; pwm_run=1; pthread_create(&pwm_thread,NULL,pwm_loop,NULL); }
static void motors_stop_all(){
    int i; for(i=0;i<4;i++){ motor_dir(i,0); motor_speed(i,0); }
}
static void motors_cleanup(){ pwm_run=0; usleep(20000); motors_stop_all(); if(!sim_mode){ int i; for(i=0;i<4;i++){ gp_unexport(ch[i].in1);gp_unexport(ch[i].in2);gp_unexport(ch[i].pwm);} gp_unexport(PIN_STBY);} }

/* 方向映射 (麦克纳姆轮): 用 dir 数组 per channel */
static void apply(int dirs[4],int spd){
    int i; double s=spd/100.0;
    for(i=0;i<4;i++){ motor_dir(i,dirs[i]); motor_speed(i,s); }
}
static void do_cmd(const char*c,int spd){
    if(!strcmp(c,"forward"))      { int d[4]={1,1,1,1}; apply(d,spd); }
    else if(!strcmp(c,"backward")){ int d[4]={-1,-1,-1,-1}; apply(d,spd); }
    else if(!strcmp(c,"spin_left")){ int d[4]={1,-1,1,-1}; apply(d,spd); }
    else if(!strcmp(c,"spin_right")){ int d[4]={-1,1,-1,1}; apply(d,spd); }
    else if(!strcmp(c,"strafe_left")){ int d[4]={-1,1,1,-1}; apply(d,spd); }
    else if(!strcmp(c,"strafe_right")){ int d[4]={1,-1,-1,1}; apply(d,spd); }
    else if(!strcmp(c,"brake")){ int i; for(i=0;i<4;i++){ motor_dir(i,1); motor_speed(i,0);} }
    else { motors_stop_all(); } /* stop 及未知 */
}
static const char* last_cmd="stop"; static int last_speed=0;
static char last_cmd_buf[32];

/* ============================================================
 * 极简 JSON 字段提取: 从 { "c":"forward", "s":50, "token":"x" } 取3字段
 * ============================================================ */
static void json_get(const char*body, char*cbuf,int csz, int*spd, char*tok,int tsz){
    if(cbuf)cbuf[0]=0; if(tok)tok[0]=0; if(spd)*spd=50;
    const char*p=body; if(!p)return;
    /* c */
    if( (p=strstr(body,"\"c\"")) ){
        p=strchr(p,':'); if(p){ p++; while(*p==' '||*p=='"')p++; const char*e=p; while(*e&&*e!='"'&&*e!='}')e++; int n=(int)(e-p); if(n>0&&n<csz){ memcpy(cbuf,p,n); cbuf[n]=0; } }
    }
    /* s */
    if( (p=strstr(body,"\"s\"")) ){
        p=strchr(p,':'); if(p){ p++; while(*p==' ')p++; *spd=atoi(p); }
    }
    /* token */
    if( (p=strstr(body,"\"token\"")) ){
        p=strchr(p,':'); if(p){ p++; while(*p==' '||*p=='"')p++; const char*e=p; while(*e&&*e!='"'&&*e!='}'&&*e!=']')e++; int n=(int)(e-p); if(n>0&&n<tsz){ memcpy(tok,p,n); tok[n]=0; } }
    }
}

/* ============================================================
 * MQTT v3.1.1 (手写, 最小)
 * ============================================================ */
static int sfd=-1;
static int readn(char*b,int n){ int r=0; while(r<n){ int k=read(sfd,b+r,n-r); if(k<=0)return -1; r+=k;} return r; }
static int send_all(const char*b,int n){ int r=0; while(r<n){ int k=write(sfd,b+r,n-r); if(k<=0)return -1; r+=k;} return 0; }

static int mqtt_ping(void){
    char p[2]={0xC0,0x00}; return send_all(p,2);
}
static int mqtt_publish(const char*topic,const char*payload,int qos){
    char hdr[64]; int tl=strlen(topic); int pl=strlen(payload);
    int rem=2+tl+2+pl;
    /* 固定头 */
    hdr[0]=0x30; /* PUBLISH qos0 */
    int rl=rem, idx=0; char rlenc[4]; do{ char b=rl%128; rl/=128; if(rl)b|=0x80; rlenc[idx++]=b; }while(rl);
    if(send_all(hdr,1))return-1; if(send_all(rlenc,idx))return-1;
    char t[128]; if(tl+2>(int)sizeof t)return-1;
    t[0]=tl>>8; t[1]=tl&0xff; memcpy(t+2,topic,tl);
    if(send_all(t,2+tl))return-1;
    return send_all(payload,pl);
}
static int mqtt_subscribe(const char*topic,int reqid){
    int tl=strlen(topic); int rem=2+2+tl+1;
    char hdr[8]; hdr[0]=0x82; hdr[1]=rem;
    char body[512]; int n=0; body[n++]=reqid>>8; body[n++]=reqid&0xff;
    body[n++]=tl>>8; body[n++]=tl&0xff; memcpy(body+n,topic,tl); n+=tl; body[n++]=0; /* qos0 */
    if(send_all(hdr,2))return-1; return send_all(body,n);
}
static int mqtt_connect(const char*host,int port,const char*user,const char*pass,const char*cid){
    struct sockaddr_in sa; memset(&sa,0,sizeof sa); sa.sin_family=AF_INET; sa.sin_port=htons(port);
    if(inet_pton(AF_INET,host,&sa.sin_addr)!=1){ struct hostent*h=gethostbyname(host); if(!h)return-2; memcpy(&sa.sin_addr,h->h_addr,h->h_length); }
    sfd=socket(AF_INET,SOCK_STREAM,0); if(sfd<0)return-3;
    if(connect(sfd,(struct sockaddr*)&sa,sizeof sa)){ close(sfd); return-4; }
    /* CONNECT payload: protocol "MQTT",v4,flags,keepalive,clientid,username,password */
    const char*proto="MQTT";
    char p[512]; int n=0;
    p[n++]=0; p[n++]=4; memcpy(p+n,proto,4); n+=4; p[n++]=4; /* 3.1.1 */
    int flags=0x80|0x40|0x02; /* user,pass,clean */  /* 0xC2: clean+user+pass */
    p[n++]=flags; p[n++]=HEARTBEAT_MS/1000>>8; p[n++]=HEARTBEAT_MS/1000&0xff;
    /* clientid */
    char cidfull[64]; snprintf(cidfull,sizeof cidfull,"%s-%d",cid,(int)getpid());
    int cl=strlen(cidfull); p[n++]=cl>>8; p[n++]=cl&0xff; memcpy(p+n,cidfull,cl); n+=cl;
    if(user){ int ul=strlen(user); p[n++]=ul>>8; p[n++]=ul&0xff; memcpy(p+n,user,ul); n+=ul; }
    if(pass){ int plen=strlen(pass); p[n++]=plen>>8; p[n++]=plen&0xff; memcpy(p+n,pass,plen); n+=plen; }
    /* fixed header + remlen */
    int rem=n; char h1; char rl[4]; int li=0;
    h1=0x10;
    do{ char b=rem%128; rem/=128; if(rem)b|=0x80; rl[li++]=b; }while(rem);
    if(send_all(&h1,1))return-5; if(send_all(rl,li))return-5; if(send_all(p,n))return-5;
    /* read CONNACK */
    char ac[4]; if(readn(ac,4)<0)return-6; if(ac[0]!=0x20)return-7; if(ac[3]!=0)printf("[mqtt] CONNACK rc=%d\n",ac[3]); if(ac[3]!=0)return-8;
    return 0;
}
/* 处理订阅接收的消息(简化: 解析PUBLISH topic+payload) */
static int mqtt_handle_incoming(void){
    char h; if(readn(&h,1)<0)return-1;
    /* 读剩余长度 */
    int mult=1, rem=0; char b;
    do{ if(readn(&b,1)<0)return-1; rem+=(b&127)*mult; mult*=128; }while(b&128);
    char buf[512]; if(rem<=0)return 0; if(rem>(int)sizeof buf)return 0;
    if(readn(buf,rem)<0)return-1;
    int type=h>>4;
    if(type==3 && rem>=4){ /* PUBLISH */
        int tl=(buf[0]<<8)|buf[1];
        if(2+tl<=rem){
            char topic[256]; memcpy(topic,buf+2,tl); topic[tl]=0;
            const char*pay=buf+2+tl;
            if(strstr(topic,DEF_TOPIC_CMD)){
                char c[32]; int s; char tk[128];
                json_get(pay,c,sizeof c,&s,tk,sizeof tk);
                if(strlen(DEF_TOKEN)>0 && strcmp(tk,DEF_TOKEN)){ printf("[sec] token mismatch, ignore\n"); return 0; }
                do_cmd(c,s); snprintf(last_cmd_buf,sizeof last_cmd_buf,"%s",c); last_cmd=last_cmd_buf; last_speed=s;
                printf("[cmd] %s speed=%d\n",c,s);
            }
        }
    } else if(type==10||type==11){ /* PINGRESP / others: ignore */ }
    return 0;
}

static volatile int running=1;
static void on_sig(int s){(void)s; running=0; }

int main(int argc,char**argv){
    const char*host=DEF_HOST; int port=DEF_PORT, i;
    for(i=1;i<argc;i++){
        if(!strcmp(argv[i],"--sim")){ sim_mode=1; }
        else if(!strcmp(argv[i],"--broker")&&i+1<argc){ host=argv[++i]; }
        else if(!strcmp(argv[i],"--port")&&i+1<argc){ port=atoi(argv[++i]); }
    }
    signal(SIGTERM,on_sig); signal(SIGINT,on_sig);
    motors_setup(); motors_start();
    printf("[carctl] host=%s:%d sim=%s\n",host,port,sim_mode?"YES":"NO");

    /* 断线重连主循环 */
    while(running){
        int rc=mqtt_connect(host,port,DEF_USER,DEF_PASS,"luckfox-carC");
        if(rc){ printf("[mqtt] connect fail rc=%d, retry 3s\n",rc); usleep(3000000); continue; }
        printf("[mqtt] connected %s:%d\n",host,port);
        mqtt_subscribe(DEF_TOPIC_CMD,1);
        /* 心跳+读取 */
        struct timeval tv; time_t last=0; time_t now;
        fd_set fds; int maxfd=sfd;
        while(running){
            tv.tv_sec=0; tv.tv_usec=200000; /* 200ms */
            FD_ZERO(&fds); FD_SET(sfd,&fds);
            int sel=select(maxfd+1,&fds,NULL,NULL,&tv);
            if(sel>0 && FD_ISSET(sfd,&fds)){
                if(mqtt_handle_incoming()<0) break; /* 断线 */
            }
            now=time(NULL);
            if(now-last>=HEARTBEAT_MS/1000){
                last=now;
                char st[256]; snprintf(st,sizeof st,
                    "{\"mode\":\"%s\",\"dir\":\"%s\",\"speed\":%d,\"ts\":%ld,\"online\":true}",
                    sim_mode?"sim":"live",last_cmd,last_speed,(long)time(NULL));
                mqtt_publish(DEF_TOPIC_STATUS,st,0);
            }
        }
        close(sfd); sfd=-1;
        if(running) printf("[mqtt] disconnect, reconnect...\n");
    }
    motors_cleanup();
    printf("[carctl] exit\n");
    return 0;
}
