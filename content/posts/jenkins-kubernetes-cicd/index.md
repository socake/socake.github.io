---
title: "Jenkins + Kubernetes：动态 Agent 构建与流水线最佳实践"
date: 2026-04-11T09:30:00+08:00
draft: false
tags: ["Jenkins", "CI/CD", "Kubernetes", "DevOps", "Pipeline"]
categories: ["CI/CD"]
description: "详解 Jenkins 在 Kubernetes 上的部署与动态 Pod Agent 配置，包括多容器 Pod Template、Shared Library 复用、kaniko 无特权构建，以及生产环境中遇到的各类踩坑经验。"
summary: "静态 Jenkins Slave 的资源浪费和配置混乱问题，在 Kubernetes 动态 Pod Agent 模式下得到根本解决。本文记录在真实生产环境中把 Jenkins 迁移到 K8s 的完整过程。"
toc: true
math: false
diagram: false
keywords: ["Jenkins", "Kubernetes", "动态Agent", "Pod Template", "Shared Library", "kaniko"]
params:
  reading_time: true
---

在把 Jenkins 迁移到 Kubernetes 之前，我们维护着一堆静态 Slave 节点：Java 项目用一组，Python 项目用另一组，前端项目再来一组。每次有新项目接入都要申请机器、装依赖、配 Jenkins 节点。更糟糕的是，这些 Slave 大部分时间处于空闲状态，但机器费用照单全收。

换成 K8s 动态 Pod Agent 之后，一个 Pod 就是一个隔离的构建环境，用完即销毁，资源利用率提升明显，配置也统一了很多。

## 为什么要用动态 Pod Agent

静态 Slave 的核心问题：

1. **环境污染**：多个项目共享同一个 Slave，A 项目安装的依赖可能和 B 项目冲突
2. **资源浪费**：空闲时 Slave 还在跑着，占用 CPU 和内存
3. **扩容慢**：并发 job 多了只能手动加 Slave 节点，扩容是分钟级甚至小时级
4. **配置漂移**：Slave 机器手工维护，时间久了各节点配置不一致

K8s Pod Agent 的优势：
- 每个 job 都在干净的容器里运行，环境完全隔离
- job 结束 Pod 自动删除，不占用资源
- 利用 K8s 弹性扩缩容，高峰期自动多起几个 Pod
- 通过 Pod Template 声明式定义构建环境，版本化管理

## Jenkins 在 K8s 上的部署

### Helm 部署

```bash
helm repo add jenkins https://charts.jenkins.io
helm repo update

helm install jenkins jenkins/jenkins \
  --namespace jenkins \
  --create-namespace \
  -f jenkins-values.yaml
```

`jenkins-values.yaml` 关键配置：

```yaml
controller:
  # 持久化 Jenkins 主目录
  persistence:
    enabled: true
    storageClass: "gp3"
    size: 50Gi
  
  # 资源限制
  resources:
    requests:
      cpu: "500m"
      memory: "1Gi"
    limits:
      cpu: "2"
      memory: "4Gi"
  
  # 初始化时自动安装插件
  installPlugins:
    - kubernetes:latest
    - workflow-aggregator:latest
    - git:latest
    - credentials-binding:latest
    - gitlab-plugin:latest
    - sonar:latest
    - email-ext:latest
  
  # Ingress 暴露
  ingress:
    enabled: true
    ingressClassName: nginx
    hostName: jenkins.example.com
    tls:
      - secretName: jenkins-tls
        hosts:
          - jenkins.example.com
  
  # JVM 参数优化
  javaOpts: "-Xms1g -Xmx3g -XX:+UseG1GC -Dfile.encoding=UTF-8"

agent:
  # Agent 默认在哪个 namespace 创建 Pod
  namespace: jenkins-agents
  
  # 允许使用自定义 Pod Template
  podTemplates: {}
```

### 持久化存储的重要性

Jenkins Master 有两类数据需要持久化：
- `JENKINS_HOME`：所有 job 配置、构建历史、插件
- workspace：当前正在构建的工作空间（可以不持久化，但 agent 需要访问）

如果只用 `emptyDir`，重启 Jenkins Pod 就会丢失所有配置。生产环境务必挂载 PVC：

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: jenkins-pvc
  namespace: jenkins
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: gp3
  resources:
    requests:
      storage: 50Gi
```

## Kubernetes Plugin 配置

安装好 Kubernetes 插件后，在 Jenkins 管理界面配置 K8s 集群连接：

**路径**：Manage Jenkins → Configure System → Cloud → Add a new cloud → Kubernetes

关键配置项：
- **Kubernetes URL**：如果 Jenkins 也在 K8s 里，直接填 `https://kubernetes.default.svc`
- **Credentials**：In-cluster 模式不需要额外凭证，Jenkins Pod 的 ServiceAccount 自动提供
- **Jenkins URL**：`http://jenkins.jenkins.svc.cluster.local:8080`（集群内通信用 Service DNS）
- **Pod Labels**：给 agent Pod 加上统一标签，方便 NetworkPolicy 控制

**RBAC 配置**，Jenkins ServiceAccount 需要在 agent namespace 里创建 Pod：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: jenkins-agent-role
  namespace: jenkins-agents
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/exec", "pods/log", "secrets", "configmaps"]
    verbs: ["get", "list", "watch", "create", "delete", "patch", "update"]
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: jenkins-agent-binding
  namespace: jenkins-agents
subjects:
  - kind: ServiceAccount
    name: jenkins
    namespace: jenkins
roleRef:
  kind: Role
  name: jenkins-agent-role
  apiGroup: rbac.authorization.k8s.io
```

## Pod Template 配置

Pod Template 定义了 agent Pod 的规格，可以在界面配置，也可以直接在 Jenkinsfile 里用代码定义（推荐代码化）。

### 多容器 Pod Template

一个 Pod 里可以跑多个 container，它们共享网络和 workspace volume，这是 K8s agent 最强大的特性：

```groovy
// Jenkinsfile
pipeline {
  agent {
    kubernetes {
      yaml """
apiVersion: v1
kind: Pod
metadata:
  labels:
    app: jenkins-agent
spec:
  serviceAccountName: jenkins-agent
  
  # 拉取私有镜像的 Secret
  imagePullSecrets:
    - name: ecr-regcred
  
  containers:
    # JNLP 容器：负责和 Jenkins Master 通信，必须有
    - name: jnlp
      image: jenkins/inbound-agent:latest-jdk17
      resources:
        requests:
          cpu: 100m
          memory: 256Mi
    
    # Maven 构建容器
    - name: maven
      image: maven:3.9-eclipse-temurin-17
      command:
        - sleep
      args:
        - infinity
      resources:
        requests:
          cpu: 500m
          memory: 1Gi
        limits:
          cpu: 2
          memory: 3Gi
      env:
        - name: MAVEN_OPTS
          value: "-Xmx2g"
      volumeMounts:
        # Maven 本地仓库缓存，挂载到宿主机目录加速
        - name: maven-repo
          mountPath: /root/.m2/repository
    
    # kaniko 构建镜像
    - name: kaniko
      image: gcr.io/kaniko-project/executor:v1.21.0-debug
      command:
        - sleep
      args:
        - infinity
      resources:
        requests:
          cpu: 500m
          memory: 512Mi
        limits:
          cpu: 2
          memory: 2Gi
      # kaniko 不需要 privileged
      securityContext:
        runAsUser: 0
    
    # kubectl 操作 K8s
    - name: kubectl
      image: bitnami/kubectl:1.29
      command:
        - sleep
      args:
        - infinity
      resources:
        requests:
          cpu: 100m
          memory: 128Mi
  
  volumes:
    # Maven 本地仓库缓存，用 hostPath 持久化
    - name: maven-repo
      hostPath:
        path: /var/jenkins/maven-repo
        type: DirectoryOrCreate
"""
    }
  }
  
  // ... stages
}
```

**注意**：用 hostPath 缓存 Maven 本地仓库有一个副作用，不同版本的依赖可能残留在宿主机上，长期不清理会占用大量空间。我们用了一个 CronJob 每周清理超过 30 天的缓存文件。

## Jenkinsfile 完整示例

```groovy
def IMAGE_NAME = "123456789.dkr.ecr.us-west-2.amazonaws.com/my-service"
def IMAGE_TAG = "${env.GIT_COMMIT[0..7]}"

pipeline {
  agent {
    kubernetes {
      // 引用预定义的 Pod Template，避免 Jenkinsfile 过长
      inheritFrom 'maven-kaniko-kubectl'
      // 也可以在这里 override 特定 container 的配置
    }
  }
  
  options {
    // 构建超时 30 分钟
    timeout(time: 30, unit: 'MINUTES')
    // 保留最近 10 次构建记录
    buildDiscarder(logRotator(numToKeepStr: '10'))
    // 同一分支不并发构建
    disableConcurrentBuilds()
  }
  
  environment {
    // 从 Jenkins Credentials 注入
    SONAR_TOKEN = credentials('sonar-token')
    AWS_CREDENTIALS = credentials('aws-ecr-credentials')
  }
  
  stages {
    stage('Checkout') {
      steps {
        checkout scm
        // 输出 git 信息，方便排查
        sh 'git log --oneline -5'
      }
    }
    
    stage('Unit Test') {
      steps {
        container('maven') {
          sh '''
            mvn test \
              -Dmaven.test.failure.ignore=false \
              -Dsurefire.useFile=false
          '''
        }
      }
      post {
        always {
          junit 'target/surefire-reports/**/*.xml'
        }
      }
    }
    
    stage('Code Quality') {
      steps {
        container('maven') {
          sh '''
            mvn sonar:sonar \
              -Dsonar.host.url=https://sonar.example.com \
              -Dsonar.login=$SONAR_TOKEN \
              -Dsonar.projectKey=${JOB_NAME}
          '''
        }
      }
    }
    
    stage('Build JAR') {
      steps {
        container('maven') {
          sh 'mvn package -DskipTests -Dmaven.javadoc.skip=true'
        }
      }
    }
    
    stage('Build & Push Image') {
      when {
        anyOf {
          branch 'main'
          branch 'develop'
        }
      }
      steps {
        container('kaniko') {
          sh """
            # 配置 ECR 认证（IRSA 模式，自动获取临时凭证）
            mkdir -p /kaniko/.docker
            cat > /kaniko/.docker/config.json << 'EOF'
{
  "credHelpers": {
    "123456789.dkr.ecr.us-west-2.amazonaws.com": "ecr-login"
  }
}
EOF
            
            /kaniko/executor \\
              --context . \\
              --dockerfile Dockerfile \\
              --destination ${IMAGE_NAME}:${IMAGE_TAG} \\
              --destination ${IMAGE_NAME}:${BRANCH_NAME} \\
              --cache=true \\
              --cache-repo=${IMAGE_NAME}/cache
          """
        }
      }
    }
    
    stage('Deploy to Staging') {
      when {
        branch 'develop'
      }
      steps {
        container('kubectl') {
          withCredentials([file(credentialsId: 'staging-kubeconfig', variable: 'KUBECONFIG')]) {
            sh """
              kubectl set image deployment/my-service \\
                my-service=${IMAGE_NAME}:${IMAGE_TAG} \\
                -n staging
              kubectl rollout status deployment/my-service -n staging --timeout=5m
            """
          }
        }
      }
    }
    
    stage('Deploy to Production') {
      when {
        branch 'main'
      }
      // 生产部署需要人工确认
      input {
        message "确认部署到生产环境？"
        ok "Deploy"
        parameters {
          string(name: 'REASON', description: '部署原因')
        }
      }
      steps {
        container('kubectl') {
          withCredentials([file(credentialsId: 'prod-kubeconfig', variable: 'KUBECONFIG')]) {
            sh """
              kubectl set image deployment/my-service \\
                my-service=${IMAGE_NAME}:${IMAGE_TAG} \\
                -n production
              kubectl rollout status deployment/my-service -n production --timeout=10m
            """
          }
        }
      }
    }
  }
  
  post {
    success {
      emailext(
        subject: "[SUCCESS] ${JOB_NAME} #${BUILD_NUMBER}",
        body: "构建成功：${BUILD_URL}",
        to: 'team@example.com'
      )
    }
    failure {
      emailext(
        subject: "[FAILED] ${JOB_NAME} #${BUILD_NUMBER}",
        body: "构建失败，请查看：${BUILD_URL}",
        to: 'team@example.com'
      )
    }
    always {
      // 清理 workspace，避免磁盘占满
      cleanWs()
    }
  }
}
```

## Shared Library 复用 Pipeline 逻辑

当项目多了之后，每个 Jenkinsfile 里都写相似的逻辑会很难维护。Shared Library 可以把公共逻辑抽取出来。

### 目录结构

在 GitLab 创建一个 `jenkins-shared-library` 仓库：

```
jenkins-shared-library/
├── src/
│   └── com/example/
│       ├── Docker.groovy      # 镜像构建封装
│       └── Notify.groovy      # 通知封装
├── vars/
│   ├── buildAndPush.groovy    # 全局函数：构建并推送镜像
│   ├── deployToK8s.groovy     # 全局函数：部署到 K8s
│   └── standardPipeline.groovy # 标准 pipeline 模板
└── resources/
    └── pod-templates/
        └── maven-kaniko.yaml  # Pod Template YAML
```

`vars/buildAndPush.groovy`：

```groovy
def call(Map config = [:]) {
  def registry = config.registry ?: '123456789.dkr.ecr.us-west-2.amazonaws.com'
  def imageName = config.imageName ?: env.JOB_NAME
  def imageTag = config.imageTag ?: env.GIT_COMMIT[0..7]
  
  container('kaniko') {
    sh """
      mkdir -p /kaniko/.docker
      echo '{"credHelpers":{"${registry}":"ecr-login"}}' > /kaniko/.docker/config.json
      
      /kaniko/executor \\
        --context . \\
        --dockerfile ${config.dockerfile ?: 'Dockerfile'} \\
        --destination ${registry}/${imageName}:${imageTag} \\
        --cache=true \\
        --cache-repo=${registry}/${imageName}/cache
    """
  }
}
```

`vars/standardPipeline.groovy`：

```groovy
def call(Map config = [:]) {
  pipeline {
    agent {
      kubernetes {
        yaml libraryResource('pod-templates/maven-kaniko.yaml')
      }
    }
    
    stages {
      stage('Test') {
        steps {
          container('maven') {
            sh 'mvn test'
          }
        }
      }
      
      stage('Build & Push') {
        when { branch 'main' }
        steps {
          buildAndPush(imageName: config.serviceName)
        }
      }
      
      stage('Deploy') {
        when { branch 'main' }
        steps {
          deployToK8s(
            namespace: config.namespace ?: 'production',
            deployment: config.serviceName
          )
        }
      }
    }
  }
}
```

业务项目的 Jenkinsfile 就变得非常简洁：

```groovy
@Library('jenkins-shared-library') _

standardPipeline(
  serviceName: 'my-service',
  namespace: 'production'
)
```

在 Jenkins 中配置 Shared Library：Manage Jenkins → Configure System → Global Pipeline Libraries，填入仓库地址即可。

## 踩坑记录

**坑1：Agent Pod 启动慢，job 长时间排队**

症状：提交 job 后，agent Pod 需要 2-3 分钟才能 Running，整体 pipeline 执行时间很长。

原因：
1. 镜像拉取慢，jnlp + maven + kaniko 三个镜像加起来好几 GB
2. 节点没有镜像缓存，每次都要重新拉取

解法：
- 在 Pod Template 里把 `imagePullPolicy` 改为 `IfNotPresent`（默认是 `Always`）
- 预先在每个节点拉取常用基础镜像（用 DaemonSet 来做）
- 对于 Maven 项目，考虑用 `mvn dependency:go-offline` 把依赖打进 agent 镜像

**坑2：多 container 之间 workspace 共享**

症状：maven container 编译产生的 JAR，在 kaniko container 里找不到。

原因：Kubernetes plugin 默认会在所有 container 里挂载同一个 workspace volume，但需要确保 workspace 目录路径一致。

解法：检查 Pod Template 里 workspace 的 mountPath，默认是 `/home/jenkins/agent`。在每个 container 里执行 `ls /home/jenkins/agent` 确认是否看到相同文件。

如果 container 的 workdir 不同，需要显式 cd：

```groovy
container('kaniko') {
  dir('/home/jenkins/agent') {
    sh '/kaniko/executor --context . ...'
  }
}
```

**坑3：凭证注入失败**

症状：`withCredentials` 块里的变量是空的，或者 `credentials()` 报找不到。

原因：
- 凭证 ID 拼写错误
- 凭证 scope 是 folder 级别，当前 job 不在这个 folder 下
- agent Pod 的 ServiceAccount 没有读取 K8s Secret 的权限（如果凭证存在 K8s Secret 里）

解法：先在 Jenkins UI 里手动测试凭证是否可以绑定，确认 ID 正确。然后检查 RBAC。

**坑4：pipeline 在 input 等待时 agent Pod 被回收**

症状：pipeline 等待人工确认时，超过一定时间后 agent Pod 被 K8s 回收，恢复执行后报 Pod 不存在。

原因：Jenkins 的 Pod 默认活跃时间限制（`activeDeadlineSeconds`）到期后，Pod 被强制删除。

解法：把 `input` 步骤放在 `node` 之外，或者单独用一个 agent-less stage：

```groovy
stage('Approval') {
  agent none  // 这个 stage 不需要 agent，不会占用 Pod
  steps {
    input message: '确认部署？'
  }
}
```

---

动态 Pod Agent 模式跑稳之后，我们的 Jenkins 节点从 8 台静态 Slave 缩减到 0，全部换成 K8s 动态 Pod。高峰期并发构建 30+ 个 job 没有问题，K8s 弹性扩容自动处理，构建环境也因为容器化彻底解决了"在我机器上能跑"的问题。
