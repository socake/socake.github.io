---
title: "Cka Topic Skills 1"
date: 2025-12-01T11:23:54+08:00
draft: false
tags: []
categories: ["Cka"]
author: "Wenzhuo Huang"
description: ""
featured_image: ""
toc: true
math: false
diagram: false
keywords: []
params:
  reading_time: true                 
---
{{ .TableOfContents }}

[Kubernetes知识图谱| ProcessOn免费在线作图,在线流程图,在线思维导图](https://www.processon.com/view/link/5ac64532e4b00dc8a02f05eb#map)

[云原生工具系列](https://github.com/liumiaocn/easypack)

[k8s中文社区推荐文章](https://mp.weixin.qq.com/s/msK9vVBxygTNqgajLSnAfQ)

# 一、技巧

1. 复制粘贴

   ```bash
   # 终端面板
   ctrl+shift+c/v
   
   # 除了终端外其他界面
   ctrl+c/v
   ```

2. 别名

   ```bash
   alias k=kubectl
   ```

3. kubectl 自动补全(（已经不需要手动设置了，默认已有）。)

   ```bash
   echo "source <(kubectl completion bash)" >> ~/.bashrc 
   source ~/.bashrc 
   ```

   ![image-20250707210413756](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202507072104845.png)

   > **`source <(kubectl completion bash)`**:  这条命令会在你的当前 shell 中执行生成的 `kubectl` 自动补全脚本。`source` 命令（也可用`.`代替）会读取并执行指定文件中的命令
   >
   > 注意写命令时不要有空格例如"source< (....)" 应写做“source<(....)”

   ```bash
    # 或进行手动安装
    yum -y install bash-completion
   
    source /usr/share/bash-completion/bash_completion
   
    source <(kubectl completion bash)
   
    echo "source <(kubectl completion bash)" >> ~/.bashrc
   ```

4. yaml模版生成`--dry-run=client -o yaml`

   ```bash
   kubectl create deploy xxx --image=nginx --dry-run=client -o yaml  > xx.yaml
   ```

   ![image-20250707210736204](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202507072107295.png)

5. 强制停止，越过优雅停止

   ```bash
   export now="--force --grace-period 0"
   k delete pod x $now
   ```

   ![image-20250707212212268](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202507072122370.png)

6. 查看帮助

   ```bash
   k create clusterrole --help
   k create rolebinding --help
   k scale --help
   k top pods --help
   k logs --help
   k drain --help
   ```

7. 查看模`kubectl explain [resource[.field]]`

8. 打开记事本，yaml改完再vim粘贴进去

# 二、题目集（一）

## 1. 基于角色控制的访问控制-RBAC(4分)

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504051144041.png" alt="img" style="zoom:50%;" />

### 1.1 中文解释：

创建一个名为deployment-clusterrole的clusterrole，该clusterrole只允许创建Deployment、Daemonset、Statefulset的create操作
在名字为app-team1的namespace下创建一个名为cicd-token的serviceAccount，并且将上一步创建clusterrole的权限绑定到该serviceAccount

### 1.2 参考

[使用 RBAC 鉴权 | Kubernetes](https://kubernetes.io/zh-cn/docs/reference/access-authn-authz/rbac/)

[为 Pod 配置服务账号 | Kubernetes](https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/configure-service-account/)

### 1.3  解题

```bash
# 修改默认命名空间
kubectl config get-context
kubectl config set-context --current --namespace xxx
```

```bash
# 创建clusterrole
 kubectl create clusterrole deploy-clusterrole --verb=create --resource=deployments,statefulsets,daemonsets
 
 # 使用yaml文件创建
 [root@k8s-master01 ~]# cat dp-clusterrole.yaml 
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: deployment-clusterrole
rules:
- apiGroups: ["extensions", "apps"]
  resources: ["deployments","statefulsets","daemonsets"]
  verbs: ["create"]
[root@k8s-master01 ~]# kubectl create -f dp-clusterrole.yaml 
clusterrole.rbac.authorization.k8s.io/deployment-clusterrole created

# 创建serviceAccount
kubectl create  sa cicd-token -n app-team1
serviceaccount/cicd-token created

# 绑定权限
kubectl create rolebinding deployment-rolebinding --clusterrole=deployment-clusterrole --serviceaccount=app-team1:cicd-token -n app-team1

# 或者使用yaml文件
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: deployment-rolebinding
  namespace: app-team1
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: deployment-clusterrole
subjects:
- kind: ServiceAccount
  name: cicd-token
  namespace: app-team1

# 验证
kubectl auth can-i create deployment --as system:serviceaccount:app-team1:cicd-token -n app-team1
yes

kubectl auth can-i create deamonset --as system:serviceaccount:app-team1:cicd-token -n app-team1
yes 

kubectl auth can-i create statefulset --as system:serviceaccount:app-team1:cicd-token -n app-team1
yes

kubectl auth can-i create pod --as system:serviceaccount:app-team1 -n app-team1
no

```

### 1.4 实践

clusterrol deployment-clusterrole

serviceAccoun cicd-token

rolebinding deployment-rolebinding  

1. 创建clusterrole
   ![image-20250708190834673](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202507081908769.png)

2. 创建serviceaccount

   ![image-20250708191103598](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202507081911666.png)

3. 绑定--rolebinding

   ![image-20250708191619179](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202507081916365.png)

4. 验证![image-20250708193210968](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202507081932061.png)

5. 清理

   ```bash
   kubectl delete clusterrole deployment-clusterrole
   kubectl delete serviceAccount cicd-token
   kubectl delete rolebinding deployment-rolebinding 
   ```

   

## 2. 节点维护（4分）

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504051324782.png" alt="img" style="zoom:50%;" />

### 2.1 中文解释：

将ek8s-node-1节点设置为不可用，然后重新调度该节点上的所有Pod

### 2.2 参考

https://kubernetes.io/zh/docs/tasks/configure-pod-container/
https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/kubeadm/kubeadm-upgrade/
https://kubernetes.io/docs/reference/generated/kubectl/kubectl-commands#drain

### 2.3 解题

```bash
kubectl cordon ek8s-node-1

# 测试执行1
kubectl drain ek8s-node-1 --delete-emptydir-data --ignore-daemonset --force --dry-run=server

# 腾空
kubectl drain ek8s-node-1 --delete-emptydir-data --igonre-daemonset --force
```

### 2.4 实践

![image-20250708194911280](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202507081949395.png)

## 3. k8s组件升级（7分）

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504051331711.png" alt="img" style="zoom: 50%;" />

### 3.1 参考

```bash
https://kubernetes.io/zh/docs/tasks/administer-cluster/kubeadm/kubeadm-upgrade/
https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/safely-drain-node/
```

### 3.2 解题

```bash
# 设置维护状态
kubectl cordon k8s-master 

# 驱逐pod
kubectl drain k8s-master --delete-emptydit-data --ignore-deamonset --force 

# 按题目介绍，ssh连接到一个master节点
ssh master01 
sudo su - 
apt update 
apt-cache policy kubeadm |grep 1.19.0 
apt install kubeadm=1.19.0-00 [--allow-change-held-packages] -y 

# 验证升级计划
kubuadm upgrade plan 

# 看到以下信息，可升级到指定版本
You can now apply the upgrade by executing the following command:

	kubeadm upgrade apply v1.19.0
_____________________________________________________________________

# 开始升级master节点,需要留意是否需要升级etcd
kuubadm upgrade apply v1.19.0 --etcd-upgrade=flase
[upgrade/successful] SUCCESS! Your cluster was upgraded to "v1.19.0". Enjoy!

[upgrade/kubelet] Now that your control plane is upgraded, please proceed with upgrading your kubelets if you haven't already done so.

# 升级kubelet和kubeproxy
apt-get install -y kubelet=1.19.0-00 kubectl=1.19.0-00 [--allow-change-held-packages]
systemctl deamon-reload
systemctl restart kubelet

# 恢复节点
kubectl uncrodon k8s-master
node/k8s-master uncordoned

kubectl get node
NAME           STATUS     ROLES                  AGE   VERSION
k8s-master01   NotReady   control-plane,master   11d   v1.19.0
k8s-node01     Ready      <none>                 8d    v1.18.8
k8s-node02     Ready      <none>                 11d   v1.18.8
kubectl get node
NAME           STATUS   ROLES                  AGE   VERSION
k8s-master01   Ready    control-plane,master   11d   v1.19.0
k8s-node01     Ready    <none>                 8d    v1.18.8
k8s-node02     Ready    <none>                 11d   v1.18.8

```

## 4. ETCD备份及恢复

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504051927353.png" alt="img" style="zoom:50%;" />

### 4.1 中文解释

中文解释：
针对etcd实例`https://127.0.0.1:2379`创建一个快照，保存到 `/srv/data/etcd-snapshot.db`。在创建快照的过程中，如果卡住了，就键入ctrl+c终止，然后重试。
然后恢复一个已经存在的快照：`/var/lib/backup/etcd-snapshot-previous.db`
执行etcdctl命令的证书存放在：
ca证书：`/opt/KUIN00601/ca.crt`
客户端证书：`/opt/KUIN00601/etcd-client.crt`
客户端密钥：`/opt/KUIN00601/etcd-client.key`

### 4.2 参考

```bash
https://kubernetes.io/zh/docs/tasks/administer-cluster/configure-upgrade-etcd/
```

### 4.3 解题

- kubernetes的所有数据记录在etcd中，对etcd进行备份就是对集群进行备份。
- 连接etcd需要证书，证书可以从apiserver获取，因为apiserver可以去连etcd。
- 新版本的apiserver都是以static pod方式运行，证书通过volume挂载到pod中。
- 具体的证书路径和备份到的路径按题目要求设置。
- ssh到master节点很快，长时间没连上，可以中断重连。 恢复部分据说很容易卡住，不要花太多时间。

```bash
# 备份
export ETCDAPI_API=3
etcdctl --endpoints="https://127.0.0.1:2379" \
		--cacert=/opt/KUIN000601/ca.crt  \
		--cert=/opt/KUIN000601/etcd-client.crt \
		--key=/opt/KUIN000601/etcd-client.key
		snapshot save \
		/srv/data/etcd-snapshot.db
		
# 还原
还原前最好关闭etcd,还原后重新开启
还原后etcd状态可能有问题，最好提前关掉
systemctl stop etcd 

mkdir /opt/backup/ -p 
cd /etc/kubernetes/manifests
mv kube-* /opt/backup

export ETCDCTL_API=3
etcdctl --endpoint="https://127.0.0.1:2379" \
		--cacert=/opt/KUIN000601/ca.crt  \
		--cert=/opt/KUIN000601/etcd-client.crt \
		--key=/opt/KUIN000601/etcd-client.key \
		snapshot restore \
		/var/lib/backup/etcd-snapshot-previous.db \
		--data-dir=/var/lib/etcd-restore

# 将volume配置的path: /var/lib/etcd改成/var/lib/etcd-restore
vim /etc/kubernetes/manifests/etcd.yaml
  volumes:
  - hostPath:
      path: /etc/kubernetes/pki/etcd
      type: DirectoryOrCreate
    name: etcd-certs
  - hostPath:
      path: /var/lib/etcd-restore
# 修改目录权限
chown etcd.etcd /var/lib/etcd-restore

# 还原etcd组件
mv /opt/backup/* /etc/kubenetes/manifests

# 还原k8s组件
mv /opt/backup/* /etc/kubetnetes/manifests
systemctl restart etcd 

```

```bash
# 其他答案
ETCDCTL_API=3 etcdctl --endpoints=https://127.0.0.1:2379 \
        --cacert=/opt/KUIN00601/ca.crt \
        --cert=/opt/KUIN00601/etcd-client.crt \
        --key=/opt/KUIN00601/etcd-client.key \
        snapshot save \
        /var/lib/backup/etcd-snapshot.db 

ETCDCTL_API=3 etcdctl --endpoints=https://127.0.0.1:2379 \
        --cacert=/opt/KUIN00601/ca.crt  \
        --cert=/opt/KUIN00601/etcd-client.crt \
        --key=/opt/KUIN00601/etcd-client.key \
        snapshot restore \
        /var/lib/backup/etcd-snapshot-previous.db 
```

> 如果是二进制安装的etcd，考试环境的etcd可能并非root用户启动的，所以可以先切换到root用户（sudo su -） 然后使用ps aux | grep etcd查看启动用户是谁和启动的配置文件是谁config-file字段指定，假设用户是etcd。所以如果是二进制安装的etcd，执行恢复时需要root权限，所以在恢复数据时，可以使用root用户恢复，之后更改恢复目录的权限：sudo chown -R etcd.etcd /var/lib/etcd-restore， 然后通过systemctl status etcd（或者ps aux | grep etcd）找到它的配置文件 （如果没有配置文件，就可以直接在etcd的service 通过systemctl status etcd即可看到文件中找到data-dir的配置），然后更改data-dir配置后，执行systemctl daemon-reload，最后使用etcd用户systemctl restart etcd即可。

## 5. NetworkPolicy(7分)

<img src="https://img2023.cnblogs.com/blog/1870449/202309/1870449-20230918113500034-1665360525.png" alt="img" style="zoom:50%;" />

### 5.1 中文解释

- 创建一个名字为allow-port-from-namespace的NetworkPolicy，这个NetworkPolicy允许internal命名空间下的Pod访问该命名空间下的9000端口。
- 不允许不是internal命令空间的下的Pod访问
- 不允许访问没有监听9000端口的Pod

### 5.2 参考

https://kubernetes.io/zh/docs/concepts/services-networking/network-policies/

### 5.3 解题

```bash
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-port-from-namespace
  namespace: internal
spec:
  ingress:
  - from:
    - podSelector: {}
    ports:
    - port: 9000
      protocol: TCP
  podSelector: {}
  policyTypes:
  - Ingress
```

![image-20250711151348326](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202507111513439.png)

**变种题目：**

- 在现有的namespace my-app中创建一个名为allow-port-from-namespace的NetworkPolicy
  确保这个NetworkPolicy允许namespace my-app中的pods可以连接到namespace big-corp中的8080。
  并且不允许不是my-app命令空间的下的Pod访问，不允许访问没有监听8080端口的Pod。
  所以可以拿着上述的答案，进行稍加修改（注意namespaceSelector的labels配置。
- 首先需要查看big-corp命名空间有没有标签：`kubectl get ns big-corp --show-labels`如果有，可以更改 `name: big-corp`为查看到的即可。
- 如果没有需要添加一个label：`kubectl label ns big-corp name=big-corp`）：

```bash
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-port-from-namespace
  namespace: my-app
spec:
  egress:
  - to:
    - namespaceSelector:
         matchLabels:
            name: big-corp
    ports:
    - protocol: TCP
      port: 8080
  ingress:
  - from:
    - podSelector: {}
    ports:
    - port: 8080
      protocol: TCP
  podSelector: {}
  policyTypes:
  - Ingress
  - Egress

```

![image-20250709194027945](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202507091940098.png)

**变种2**

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504060936541.png" alt="img" style="zoom:50%;" />

https://kubernetes.io/zh/docs/concepts/services-networking/network-policies/

```bash
# 切换到指定集群
kubectl config use-context [NAME]
# 查看 namespace corp-bar 的标签，如：kubernetes.io/metadata.name=corp-bar
kubectl get ns --show-labels

apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-port-from-namespace
  namespace: big-corp 
spec:
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: internal
    ports:
    - port: 9200
      protocol: TCP
  podSelector: {}
  policyTypes:
  - Ingress
```

## 6. Service(7分)

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504060940650.png" alt="img" style="zoom:50%;" />

### 6.1 中文解释

重新配置一个已经存在的deployment front-end，在名字为nginx的容器里面添加一个端口配置，名字为http，暴露端口号为80/TCP，然后创建一个service，名字为front-end-svc，暴露该deployment的http端口，并且service的类型为NodePort。

### 6.2 参考

https://kubernetes.io/docs/concepts/services-networking/connect-applications-service/
https://kubernetes.io/zh-cn/docs/concepts/workloads/controllers/deployment/
https://kubernetes.io/zh-cn/docs/concepts/services-networking/service/****

### 6.3 解题

```bash
kubectl edit deploy front-end
    spec:
      containers:
      - name: nginx
        image: nginx
        # 需要加这四行
        ports:
        - name: http
          containerPort: 80
          protocol: TCP
```

![img](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504060951193.png)

```bash
kubectl expose deploy front-end --name=front-end-svc --port=80 --target-port=http --type=NodePort 

# 或者通过文件方式创建service
apiVersion: v1
kind: Service
metadata:
  name: front-end-svc
  labels:
    app: front-end
spec:
  type: NodePort
  selector:
    app: front-end   # label需要匹配，否则访问不到。
  ports:
    - name: http
      protocol: TCP
      port: 80
      targetPort: 80
```

![image-20250709203839068](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202507092038205.png)

## 7. Ingress（7分）

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504061013101.png" alt="img" style="zoom:50%;" />

### 7.1 中文解释

在ing-internal 命名空间下创建一个ingress，名字为pong，代理的service hi，端口为5678，配置路径/hi。 验证：访问`curl -kL <INTERNAL_IP>/hi`会返回hi。

![](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202507101144177.svg)

> Ingress 是一种用于管理外部对集群内服务访问的资源。它可以根据 HTTP/HTTPS 请求的路径、主机名等规则，将流量路由到不同的后端服务

### 7.2 参考

https://kubernetes.io/zh/docs/concepts/services-networking/ingress/
https://kubernetes.io/zh-cn/docs/concepts/services-networking/service/

### 7.3 解题

```bash
# ingressClassName需要指定为nginx
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: pong
  namespace: ing-internal
spec:
  ingressClassName: nginx
  rules:
  - http:
      paths:
      - path: /hi
        pathType: Prefix
        backend:
          service:
            name: hi
            port:
              number: 5678
              
kubectl get ingress -n ing-internal #获取ip后使用curl验证

# 如果考试时没有出ip需要再annotation下加一行
cat ingress.yaml 
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: pong
  namespace: ing-internal
  annotations:
    nginx.ingress.kubernetes.io
    nginx.ingress.kubernetes.io/rewrite-target: /
spec:
  rules:
  - http:
      paths:
      - path: /hi
        pathType: Prefix
        backend:
          service:
            name: hi
            port:
              number: 5678
```

![image-20250710124824781](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202507101248945.png)

> ingressclassname如果不指定，则会使用集群默认的指定的ingress
>
> - `ingressClassName: nginx`：指定 Ingress 使用的控制器类名为 `nginx`。这意味着 Kubernetes 将使用 Nginx Ingress 控制器来处理这个 Ingress 规则。不同的 Ingress 控制器可能有不同的功能和配置方式，这里明确使用 Nginx Ingress 控制器。

## 8. Deployment 扩缩容（4分）

<img src="https://img2023.cnblogs.com/blog/1870449/202309/1870449-20230918113823065-1688964830.png" alt="img" style="zoom:50%;" />

### 8.1 中文解释

扩容名字为loadbalancer的deployment的副本数为6

### 8.2 参考

https://kubernetes.io/zh-cn/docs/concepts/workloads/controllers/deployment/

### 8.3 解题

```bash
kubectl scale --replicas=6 deployment localbalancer
kubectl edit deploy  localbalancer
```

![image-20250710125350375](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202507101253526.png)

## 9.  指定节点部署-调度（4分）

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504061036760.png" alt="img" style="zoom:50%;" />

### 9.1 中文解释

创建一个Pod，名字为nginx-kusc00401，镜像地址是nginx，调度到具有disk=spinning标签的节点上

### 9.2 参考

https://kubernetes.io/zh/docs/concepts/scheduling-eviction/assign-pod-node/
https://kubernetes.io/zh/docs/tasks/configure-pod-container/assign-pods-nodes/

### 9.3 解题

```bash
vim pod-ns.yaml
apiVersion: v1
kind: Pod
metadata:
  name: nginx-kusc00401
  labels:
    role: nginx-kusc00401
spec:
  nodeSelector:
    disk: spinning
  containers:
    - name: nginx
      image: nginx

kubectl create -f pod-ns.yaml

# 省时
kubectl run nginx-kusc00401 --image=nginx --dry-run=client -o yaml >9.yaml

```

![image-20250710140729589](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202507101454142.png)

## 10. 检查Node节点的健康状态（4分）

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504061039456.png" alt="img" style="zoom:50%;" />

### 10.1  中文解释

检查集群中有多少节点为Ready状态，并且去除包含NoSchedule污点的节点。之后将数字写到`/opt/KUSC00402/kusc00402.txt`

### 10.2 参考

https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/taint-and-toleration/

### 10.3 解题

~~~bash
# 记录总数为
kubectl get node |grep -i ready|wc -l
# 记录不可调度的节点
kubectl describe node |grep  -i taints |grep -i noschedule|wc -l 

# 将差值写入文件
echo x >> /opt/KUSC00402/kusc00402.txt
~~~

## 11. 一个pod多个容器（4分）

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504061046579.png" alt="img" style="zoom:50%;" />

### 11.1 中文解释

创建一个Pod，名字为kucc1，这个Pod可能包含1-4容器，该题为四个：nginx+redis+memcached+consul

### 11.2  参考

https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/

### 11.3 解题

```bash
# 使用yaml直接创建
apiVersion: v1
kind: Pod
metadata:
  name: kucc1
spec:
  containers:
  - image: nginx
    name: nginx
  - image: redis
    name: redis
  - image: memchached
    name: memcached
  - image: consul
    name: consul

# 或者用dry-run=client 命令快速生成一个yaml模版。修改模板
kubectl run kuccl --image=nginx --dry-run=client -o yaml > 11.yaml 
apiVersion: v1
kind: Pod
metadata:
  labels:
    run: kucc1
  name: kucc1
spec:
  containers:
  - image: nginx
    name: nginx
  - image: redis
    name: redis
  - image: memcached
    name: memcached
  - image: consul
    name: consul
  dnsPolicy: ClusterFirst
  restartPolicy: Always
```

## 12. PersistentVolume(4分)

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504061254207.png" alt="img" style="zoom:50%;" />

### 12.1 中文解释

创建一个pv，名字为app-config，大小为2Gi，访问权限为ReadWriteMany。Volume的类型为hostPath，路径为/srv/app-config

### 12.2 参考

https://kubernetes.io/docs/tasks/configure-pod-container/configure-persistent-volume-storage/
https://kubernetes.io/zh-cn/docs/concepts/storage/persistent-volumes/
可以ctrl+F 搜003，会直接跳转到创建pv
https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/configure-persistent-volume-storage/#create-a-persistentvolume

### 12.3 解题

```bash
apiVersion: v1
kind: PersistentVolume
metadata:
  name: app-config
  labels:
    type: local
spec:
  storageClassName: manual   # 需要有这一项吗？题目没有要求，（可以不写）
  volumeMode: Filesystem
  capacity:
    storage: 2Gi
  accessModes:
    - ReadWriteMany
  hostPath:
    path: "/srv/app-config"

kubectl get pv app-config
```

![image-20250711203852463](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202507112038579.png)

## 13. 监控pod度量指标（5分）

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504061312775.png" alt="img" style="zoom:50%;" />

### 13.1  中文解释

找出具有`name=cpu-user`的Pod，并过滤出使用CPU最高的Pod，然后把它的名字写在已经存在的 `/opt/KUTR00401/KUTR00401.txt`文件里（注意他没有说指定namespace。所以需要使用-A指定所以namespace）

### 13.2 参考

https://kubernetes.io/zh-cn/docs/reference/kubectl/

### 13.3 解题

```bash
kubectl top pod -A --use-protocol-buffers --selector "name=cpu-user" --sort-by 
NAMESPACE     NAME                       CPU(cores)   MEMORY(bytes)   
kube-system   coredns-54d67798b7-hl8xc   7m           8Mi   
kube-system   coredns-54d67798b7-m4m2q   6m           8Mi

# 此处以pod实际名称为准，在cpu列选出最大的一个，cup数值 1,2,3 > 带m ，1000m=1
echo "coredns-54d67798b7-hl8xc" >> /opt/KUTR00401/KUTR00401.txt

# 其他解法：
kubectl get pods -A --show-labels
kubectl top pods -A -l name=cpu-user --sort-by="cpu"
echo "[podname]" >> /opt/KUTR00401/KUTR00401.txt
```

## 14. 监控pod日志

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504061332807.png" alt="img" style="zoom:50%;" />

### 14.1 中文解释

监控名为foobar的Pod的日志，并过滤出具有unable-access-website信息的行，然后将写入到 /opt/KUTR00101/foobar

### 14.2 参考

https://kubernetes.io/zh-cn/docs/reference/kubectl/

### 14.3 解题

```bash
kubectl logs foobar |grep 'unable-access-website'>> /opt/KUBE0010 
```

## 15. CSI & PersistentVolumeClaim（7分）

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504061407903.png" alt="img" style="zoom:50%;" />

### 15.1 中文解释

创建一个名字为pv-volume的pvc，指定storageClass为csi-hostpath-sc，大小为10Mi
然后创建一个Pod，名字为web-server，镜像为nginx。

并且挂载该PVC至`/usr/share/nginx/html`，挂载的权限为ReadWriteOnce。之后通过 `kubectl edit`或者 `kubectl path`将pvc改成70Mi，并且记录修改记录。

### 15.2 参考

https://kubernetes.io/docs/tasks/configure-pod-container/configure-persistent-volume-storage/
https://kubernetes.io/zh-cn/docs/concepts/storage/persistent-volumes/

### 15.3 解题

```bash
# 创建PVC
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: pv-volume
spec:
  accessModes:
  - ReadWriteOnce
  volumeMode: Filesystem
  resources:
    requests:
      storage: 10Mi
  storageClassName: csi-hostpath-sc
  
# 创建pod
apiVersion: v1
kind: Pod
metadata:
  name: web-server
spec:
  containers:
    - name: nginx
      image: nginx
      volumeMounts:
      - mountPath: "/usr/share/nginx/html"
        name: pv-volume   # 名字不是必须和pvc一直，也可以为my-volume
  volumes:
    - name: pv-volume   # 名字不是必须和pvc一直，也可以为my-volume
      persistentVolumeClaim:
        claimName: pv-volume
        
# 扩容
kubectl patch pvc pv-volume -p '{"spec":{"resources":{"requests":{"storage": "70Mi"}}}}' --record

# 方式二
kubectl edit pvc pv-volume
kubectl edit pvc pv-volume --record
kubectl edit pvc pv-volume --save-config
将两处的10Mi都改为70Mi，如果是nfs会因为不支持动态扩容而失败
edit完需要等待一小会
```

![img](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504061416543.png)

## 16. sidecar(7分)

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504061417065.png" alt="img" style="zoom:50%;" />

### 16.1 中文解释

添加一个名为busybox且镜像为busybox的sidecar到一个已经存在的名为legacy-app的Pod上

这个sidecar的启动命令为 `/bin/sh, -c, 'tail -n+1 -f /var/log/legacy-app.log'`。
并且这个sidecar和原有的镜像挂载一个名为logs的volume，挂载的目录为`/var/log/`

### 16.2 参考

https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/configure-volume-storage/
https://kubernetes.io/zh-cn/docs/concepts/cluster-administration/logging/

### 16.3 解题

~~~bash
# 导出元文件
kubectl get pod legacy-app -o yaml > c-sidecar.yaml
apiVersion: v1
kind: Pod
metadata:
  name: legacy-app
spec:
  containers:
  - name: count
    image: busybox
    args:
    - /bin/sh
    - -c
    - >
      i=0;
      while true;
      do
        echo "$(date) INFO $i" >> /var/log/legacy-ap.log;
        i=$((i+1));
        sleep 1;
      done   
      
# 基于此文件添加sidecar及volume
vim c-sidecar.yaml
apiVersion: v1
kind: Pod
metadata:
  name: legacy-app
spec:
  containers:
  - name: count
    image: busybox
    args:
    - /bin/sh
    - -c
    - >
      i=0;
      while true;
      do
        echo "$(date) INFO $i" >> /var/log/legacy-ap.log;
        i=$((i+1));
        sleep 1;
      done  
    # 加上下面部分
    volumeMounts:
    - name: logs
      mountPath: /var/log
  - name: busybox
    image: busybox
    args: [/bin/sh, -c, 'tail -n+1 -f /var/log/legacy-ap.log']
    volumeMounts:
    - name: logs
      mountPath: /var/log
  volumes:
  - name: logs
    emptyDir: {}
    
# 重新应用 
kubectl delete -f c-sidecar.yaml
kubectl create -f c-sidecar.yaml

# 检查
kubectl logs legacy-app -c busybox
~~~

## 17.  集群故障排查--kubelet故障（13分）

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202504061424095.png" alt="img" style="zoom:50%;" />

### 17.1 中文解释

一个名为wk8s-node-0的节点状态为NotReady，让其他恢复至正常状态，并确认所有的更改开机自动完成

### 17.2 解题

```bash
# 检查服务状态
systemctl status kubelet
systemctl start kubelet
systemctl enable kubelet

其实这题没这么简单，一般启动kubelet后大概率是启动失败的
可能的原因：
1.kubelet二进制文件路径不对，which kubelet后和服务启动文件kubelet systemd service做个对比，看是否是这个原因
2.service文件路径和它启动的路径不一致，在启动目录下找不到service文件，可以全局搜下并做个软链接。
3.其他原因

# 再次检查wk8s-node-0是否在ready
ssh master01
kubectl get nodes
```

### 17.3集群故障排查——主节点故障(13)

这是之前的考题，现在应该没有这个题了。

参考：

```ruby
https://kubernetes.io/zh/docs/tasks/configure-pod-container/static-pod/
```



## 可能会考的题

### 题目1：nginx打标签

```undefined
labels key1=rw01 key2=rw02
```

思路：

```dockerfile
label pod/deployment
```

参考：

```avrasm
https://kubernetes.io/zh-cn/docs/concepts/cluster-administration/manage-deployment/#using-labels-effectively
```

步骤：

```lua
kubectl run hwcka-005 --image=nginx --labels key1=rw01,key2=rw02
kubectl apply -f name.yaml
```

### 题目2：deployment版本升级回退

```undefined
1.创建deployment版本nginx
2.修改镜像1.12.0，并记录这个更新
3.回退到上个版本
```

思路：

```csharp
1.deployment rollout
2.--record
```

参考：

```avrasm
https://kubernetes.io/zh-cn/docs/concepts/workloads/controllers/deployment/
https://kubernetes.io/zh-cn/docs/concepts/workloads/controllers/deployment/#rolling-back-a-deployment
```

步骤：

```lua
kubectl create deployment hwcka-07 --image=nginx --dry-run=client -o yaml > 7.yaml
kubectl apply -f 7.yaml
kubectl edit deployments.apps hwcka-07 --record    # 修改nginx镜像为nginx:1.12.0
kubectl rollout history deployment hwcka-07 
kubectl rollout undo deployment hwcka-07 --to-revision=1
# 回退前和回退后都需要edit查看下image的版本
```