/*
 * rtsp_relay.c - 纯 C RTSP 转发器 (板上跑, 把 rkipc RTSP 流 copy 推到云端 mediamtx)
 *
 * 链路: [板子 rkipc] rtsp://127.0.0.1:554/live/1  --拉流-->
 *        [本程序]  --publish-->  rtsp://YOUR_SERVER_IP:8554/car
 * 纯 RTP 转发(copy, 不转码), 内存极小, 替代本机 ffmpeg。
 *
 * 依赖: 无 (纯 POSIX socket, 静态编译可跑任何 Buildroot)
 * 编译: arm-linux-gnueabihf-gcc -O2 -s -static -o rtsp_relay rtsp_relay.c
 * 用法: ./rtsp_relay [src_rtsp] [dst_rtsp]
 * 默认: src=rtsp://127.0.0.1:554/live/1  dst=rtsp://YOUR_SERVER_IP:8554/car
 *
 * 协议说明:
 *   - 拉流(客户端): DESCRIBE->SETUP(UDP收RTP)->PLAY, 从 rkipc 收 RTP over UDP
 *   - 推流(客户端 publish): ANNOUNCE(带SDP)->SETUP->PLAY, 把RTP发到云 mediamtx 指定UDP端口
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <time.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <fcntl.h>
#include <signal.h>

#define MAXB 65536
static char rbuf[MAXB+1];
static int tcpfd=-1;
static volatile int running=1;
static void onsig(int s){(void)s;running=0;}

/* ---- TCP 简单收发 ---- */
static int tc_connect(const char*host,int port){
    int fd=socket(AF_INET,SOCK_STREAM,0); if(fd<0)return-1;
    struct sockaddr_in sa; memset(&sa,0,sizeof sa); sa.sin_family=AF_INET; sa.sin_port=htons(port);
    if(inet_pton(AF_INET,host,&sa.sin_addr)!=1){ struct hostent*h=gethostbyname(host); if(!h){close(fd);return-1;} memcpy(&sa.sin_addr,h->h_addr,h->h_length); }
    if(connect(fd,(struct sockaddr*)&sa,sizeof sa)){close(fd);return-1;}
    return fd;
}
static int tsend(int fd,const char*s){ int n=strlen(s); return send(fd,s,n,MSG_NOSIGNAL); }
/* 读一行CRLF */
static int tline(int fd,char*out,int max){
    int n=0; char c; struct timeval tv={5,0};
    while(n<max-1){
        int r=recv(fd,&c,1,0); if(r<=0)return-1;
        if(c=='\n'){ break; }
        if(c!='\r') out[n++]=c;
    }
    out[n]=0; return n;
}
/* 读响应头直到空行, status行->status, Content-Length指示的body->body(若有), 头文本拼到hdrout(可选) */
static int read_response(int fd,char*status,int stsz,char*body,int bodysz,char*hdrout,int hsz){
    char line[1024]; int first=1; int contentlen=0; int ho=0;
    while(1){
        int n=tline(fd,line,sizeof line); if(n<0)return-1;
        if(first){ snprintf(status,stsz,"%s",line); first=0; }
        if(hdrout && hsz>0 && n>0 && ho < hsz-1){
            int cp=n<(hsz-1-ho)?n:(hsz-1-ho); memcpy(hdrout+ho,line,cp); ho+=cp; hdrout[ho]='\n'; ho++; if(ho>=hsz-1)ho=hsz-1; hdrout[ho]=0;
        }
        if(n==0) break; /* 空行=头结束 */
        if(strncasecmp(line,"Content-Length:",15)==0) contentlen=atoi(line+15);
    }
    if(hdrout && hsz>0) hdrout[ho]=0;
    if(contentlen>0 && body && bodysz>0){
        int read=0;
        while(read<contentlen && read<bodysz-1){
            int r=recv(fd,body+read,contentlen-read,0); if(r<=0)break; read+=r;
        }
        body[read]=0;
        if(read<contentlen) return -2; /* body不完整 */
    } /* body==NULL 则只读头, 不读body */
    return 0;
}
static void make_auth(char*out,int sz){
    /* base64("car:YOUR_MQTT_PASSWORD") 硬编码或计算 */
    const char*u="car",*p="YOUR_MQTT_PASSWORD";
    char tmp[256]; snprintf(tmp,sizeof tmp,"%s:%s",u,p);
    /* 简易 base64 */
    static const char b64[]="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    int i=0,len=strlen(tmp); char*o=out; 
    for(i=0;i+2<len;i+=3){
        int v=(tmp[i]&255)<<16|(tmp[i+1]&255)<<8|(tmp[i+2]&255);
        *o++=b64[(v>>18)&63]; *o++=b64[(v>>12)&63]; *o++=b64[(v>>6)&63]; *o++=b64[v&63];
    }
    int rem=len-i; if(rem==1){ int v=tmp[i]<<16; *o++=b64[(v>>18)&63];*o++=b64[(v>>12)&63];*o++='=';*o++='='; }
    else if(rem==2){ int v=(tmp[i]&255)<<16|(tmp[i+1]&255)<<8; *o++=b64[(v>>18)&63];*o++=b64[(v>>12)&63];*o++=b64[(v>>6)&63];*o++='='; }
    *o=0;
}
static char auth_hdr[256];

/* ---- 从 SDP 提取 media 行 -> 第一路 video 的 payload 类型(忽略, copy) ---- */
static void* find_sdp_media(char*sdp){ return strstr(sdp,"m=video")? (void*)1 : NULL; }

/* ---------------- RTSP 客户端(拉流) ---------------- */
/* 拉端结构 */
typedef struct {
    int tcp;
    char control[1024];
    int rtp_port; /* 本地RTP收端口 */
    int rtcp_port;
    char session[256];
    char sdp[8192];
} rtx_t;

static int udp_bind(int*port){
    int fd=socket(AF_INET,SOCK_DGRAM,0); if(fd<0)return-1;
    struct sockaddr_in a; a.sin_family=AF_INET; a.sin_addr.s_addr=htonl(INADDR_ANY); a.sin_port=0;
    if(bind(fd,(struct sockaddr*)&a,sizeof a)){close(fd);return-1;}
    socklen_t l=sizeof a; getsockname(fd,(struct sockaddr*)&a,&l); *port=ntohs(a.sin_port);
    return fd;
}

/* 拉流: 建立并返回 RTP UDP fd, 以及 session id, SDP */
static int rtsp_pull(const char*url, rtx_t*r){
    char host[128]={0}; int port=554; char path[256]="/live/1";
    /* 解析 url rtsp://host:port/path */
    const char*u=url; if(!strncasecmp(u,"rtsp://",7))u+=7;
    const char*h=u; while(*h&&*h!=':'&&*h!='/')h++;
    int hl=(int)(h-u); if(hl>127)hl=127; memcpy(host,u,hl); host[hl]=0;
    if(*h==':'){ port=atoi(h+1); const char*s=strchr(h,'/'); path[0]=0; if(s)strncpy(path,s,sizeof path-1); }
    else if(*h=='/'){ path[0]=0; strncpy(path,h,sizeof path-1); }
    if(path[0]==0) strcpy(path,"/");
    /* tcp */
    tcpfd=tc_connect(host,port); if(tcpfd<0){fprintf(stderr,"[pull] connect %s:%d fail\n",host,port); return-1;}
    r->tcp=tcpfd;
    char req[2048]; char status[256]; char body[8192]={0};
    /* DESCRIBE */
    snprintf(req,sizeof req,"DESCRIBE rtsp://%s:%d%s RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n",host,port,path);
    tsend(tcpfd,req); if(read_response(tcpfd,status,sizeof status,body,sizeof body,NULL,0)){fprintf(stderr,"[pull] DESCRIBE fail %s\n",status);return-1;}
    snprintf(r->sdp,sizeof r->sdp,"%s",body);
    if(strstr(status,"200")==NULL){fprintf(stderr,"[pull] DESCRIBE %s\n",status);return-1;}
    /* SETUP video, UDP */
    char cntl[1024]; snprintf(cntl,sizeof cntl,"rtsp://%s:%d%s/track1",host,port,path);
    int rtp=0,rtcp=0; int rtpfd=udp_bind(&rtp); if(rtpfd<0)return-1; rtcp=rtp+1;
    snprintf(req,sizeof req,"SETUP %s RTSP/1.0\r\nCSeq: 2\r\nTransport: RTP/AVP/UDP;unicast;client_port=%d-%d\r\n\r\n",cntl,rtp,rtcp);
    tsend(tcpfd,req);
    char extra[2048]={0}; if(read_response(tcpfd,status,sizeof status,NULL,0,extra,sizeof extra)){fprintf(stderr,"[pull] SETUP fail\n");return-1;}
    if(strstr(status,"200")==NULL){fprintf(stderr,"[pull] SETUP %s\n",status);close(rtpfd);return-1;}
    /* 提取 session (在响应头 extra 中) */
    char*sess=strstr(extra,"Session:"); if(sess){ sscanf(sess+9,"%[^;]",r->session); }
    /* PLAY (带Session头) */
    char sessline[64]=""; if(r->session[0]) snprintf(sessline,sizeof sessline,"Session: %s\r\n",r->session);
    snprintf(req,sizeof req,"PLAY %s RTSP/1.0\r\nCSeq: 3\r\n%sRange: npt=0.000-\r\n\r\n",cntl,sessline);
    tsend(tcpfd,req);
    /* 读 PLAY 响应头(status应含200); 忽略读取返回值, 只看status */
    {
        char htmp[2048]={0};
        read_response(tcpfd,status,sizeof status,NULL,0,htmp,sizeof htmp);
        if(strstr(status,"200")==NULL){ fprintf(stderr,"[pull] PLAY %s (sess='%s')\n",status,r->session); close(rtpfd); return -1; }
    }
    return rtpfd;
}

/* ---------------- RTSP publish(推流到 mediamtx) ---------------- */
static int rtsp_publish(const char*url,const char*sdp,int pullrtp_fd){
    char host[128]={0}; int port=8554; char path[256]="/car";
    const char*u=url; if(!strncasecmp(u,"rtsp://",7))u+=7;
    const char*h=u; while(*h&&*h!=':'&&*h!='/')h++;
    int hl=(int)(h-u); if(hl>127)hl=127; memcpy(host,u,hl); host[hl]=0;
    if(*h==':'){ port=atoi(h+1); const char*s=strchr(h,'/'); path[0]=0; if(s)strncpy(path,s,sizeof path-1);}
    else if(*h=='/'){path[0]=0;strncpy(path,h,sizeof path-1);}
    if(path[0]==0) strcpy(path,"/car");
    int fd=tc_connect(host,port); if(fd<0){fprintf(stderr,"[pub] connect %s:%d fail\n",host,port);return-1;}
    char req[9000]; char status[256]; char body[8192]={0}; char extra[4096]={0};
    /* ANNOUNCE with SDP */
    snprintf(req,sizeof req,"ANNOUNCE rtsp://%s:%d%s RTSP/1.0\r\nCSeq: 1\r\nContent-Type: application/sdp\r\nAuthorization: Basic %s\r\nContent-Length: %d\r\n\r\n%s",
        host,port,path,auth_hdr,(int)strlen(sdp),sdp);
    tsend(fd,req); if(read_response(fd,status,sizeof status,NULL,0,body,sizeof body)){fprintf(stderr,"[pub] ANNOUNCE fail\n");close(fd);return-1;}
    if(strstr(status,"200")==NULL && strstr(status,"461")==NULL){fprintf(stderr,"[pub] ANNOUNCE %s\n",status);close(fd);return-1;}
    /* SETUP - mediamtx 接受 SETUP 到 rtsp://.../car/track1 */
    int rtp_local=0,rtcp_local=0; int rtpfd=udp_bind(&rtp_local); rtcp_local=rtp_local+1;
    char sessline[64]=""; char*ss=strstr(body,"Session:"); if(ss){sscanf(ss+9,"%[^;\r\n]",sessline);}
    char sess2[64]=""; if(sessline[0]) snprintf(sess2,sizeof sess2,"Session: %s\r\n",sessline);
    memset(req,0,sizeof req);
    snprintf(req,sizeof req,"SETUP rtsp://%s:%d%s/trackID=0 RTSP/1.0\r\nCSeq: 2\r\nTransport: RTP/AVP/UDP;unicast;client_port=%d-%d;mode=record\r\n%sAuthorization: Basic %s\r\n\r\n",
        host,port,path,rtp_local,rtcp_local,sess2,auth_hdr);
    tsend(fd,req); memset(extra,0,sizeof extra); 
    if(read_response(fd,status,sizeof status,NULL,0,extra,sizeof extra)){fprintf(stderr,"[pub] SETUP fail\n");close(fd);return-1;}
    if(strstr(status,"200")==NULL){fprintf(stderr,"[pub] SETUP %s\n",status);close(fd);return-1;}
    /* 解析 serve 端 rtp 端口(server_port, 在响应头 extra) */
    int srv_rtp=0; char*sp=strstr(extra,"server_port="); if(sp){ sscanf(sp+12,"%d",&srv_rtp);}
    struct sockaddr_in dst; dst.sin_family=AF_INET; dst.sin_port=htons(srv_rtp?srv_rtp:65535);
    inet_pton(AF_INET,host,&dst.sin_addr);
    /* PLAY */
    char sess3[64]=""; if(sessline[0]) snprintf(sess3,sizeof sess3,"Session: %s\r\n",sessline);
    snprintf(req,sizeof req,"PLAY rtsp://%s:%d%s RTSP/1.0\r\nCSeq: 3\r\n%sAuthorization: Basic %s\r\n\r\n",
        host,port,path,sess3,auth_hdr);
    tsend(fd,req); 
    char tmp[1024]={0}; read_response(fd,status,sizeof status,tmp,sizeof tmp,NULL,0);
    if(strstr(status,"200")==NULL && strstr(status,"461")==NULL){ fprintf(stderr,"[pub] PLAY %s\n",status); close(fd); return -1; }

    /* ---- 转发: 从 pull rtp fd recv, sendto 云 dst ---- */
    int sendfd=socket(AF_INET,SOCK_DGRAM,0);
    char buf[2048];
    struct timeval tv; tv.tv_sec=1; tv.tv_usec=0;
    while(running){
        fd_set fds; FD_ZERO(&fds); FD_SET(pullrtp_fd,&fds); FD_SET(fd,&fds);
        int max=pullrtp_fd>fd?pullrtp_fd:fd;
        int s=select(max+1,&fds,0,0,&tv); if(s<=0)continue;
        if(FD_ISSET(fd,&fds)){ /* server 发来 signal, 读掉 */
            char b[128]; recv(fd,b,sizeof b,MSG_DONTWAIT);
        }
        if(FD_ISSET(pullrtp_fd,&fds)){
            int n=recv(pullrtp_fd,buf,sizeof buf,0); if(n<=0)continue;
            if(srv_rtp){ sendto(sendfd,buf,n,0,(struct sockaddr*)&dst,sizeof dst); }
        }
    }
    close(fd); close(sendfd); close(rtpfd);
    return 0;
}

int main(int argc,char**argv){
    const char*src="rtsp://192.168.3.66:554/live/1";
    const char*dst="rtsp://YOUR_SERVER_IP:8554/car";
    if(argc>1)src=argv[1]; if(argc>2)dst=argv[2];
    make_auth(auth_hdr,sizeof auth_hdr);
    signal(SIGINT,onsig); signal(SIGTERM,onsig);
    printf("[rtsp_relay] %s -> %s\n",src,dst);
    while(running){
        rtx_t r; memset(&r,0,sizeof r);
        int pullfd=rtsp_pull(src,&r);
        if(pullfd<0){ fprintf(stderr,"[pull] fail, retry 3s\n"); usleep(3000000); continue; }
        printf("[pull] OK, sdp len=%d\n",(int)strlen(r.sdp));
        if(!find_sdp_media((char*)r.sdp)){ fprintf(stderr,"[pull] no video in sdp\n"); sleep(2); continue; }
        rtsp_publish(dst,r.sdp,pullfd);
        close(pullfd); close(r.tcp); tcpfd=-1;
        if(running) printf("[relay] disconnected, reconnect\n");
    }
    printf("[rtsp_relay] exit\n");
    return 0;
}
