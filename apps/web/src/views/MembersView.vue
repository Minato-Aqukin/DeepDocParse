<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, ref } from 'vue'

import { orgApi } from '@/api/org'
import { useAsyncData } from '@/composables/useAsyncData'
import { useAuthStore } from '@/stores/auth'
import type { Member, Organization } from '@/types/api'
import { ROLE_META, ROLE_VALUES, type Role } from '@deepdocparse/contracts'

/**
 * 成员与角色。
 *
 * **首发是单组织独占部署**：一次部署 = 一个组织 = 一份语料，成员共享全部语料。
 * 所以这一页管的是"谁能做什么"，不是"谁能看见什么" —— 检索天然跨全语料。
 *
 * 角色文案与顺序来自契约（`packages/contracts/enums.yaml` 的 role 段），
 * 不在这里手写一份。
 */
const auth = useAuthStore()

const org = ref<Organization | null>(null)
const members = ref<Member[]>([])

const { loading, refresh: reload } = useAsyncData(async () => {
  const [orgResp, memberResp] = await Promise.all([orgApi.current(), orgApi.members()])
  org.value = orgResp.data
  members.value = memberResp.data
}, undefined)

const inviting = ref(false)
const newUsername = ref('')
const newRole = ref<Role>('contributor')

/** 角色下拉的选项 —— 顺序即权限高低，取自契约的声明顺序。 */
const roleOptions = computed(() =>
  ROLE_VALUES.map((value) => ({ value, label: ROLE_META[value].label })),
)

const admins = computed(() => members.value.filter((m) => m.role === 'admin'))

/**
 * 最后一个管理员不能被降级或移除。
 *
 * 服务端也会拒（409 `last_admin`）—— 这里只是**提前把按钮变灰并说明原因**。
 * 让用户点下去再吃一个错误也能工作，但那会让人以为是自己操作错了。
 */
function isLastAdmin(member: Member): boolean {
  return member.role === 'admin' && admins.value.length <= 1
}

async function addMember() {
  if (!newUsername.value.trim()) return ElMessage.warning('先填用户名')
  inviting.value = true
  try {
    await orgApi.addMember(newUsername.value.trim(), newRole.value)
    newUsername.value = ''
    await reload()
    ElMessage.success('已加入组织')
  } finally {
    inviting.value = false
  }
}

async function changeRole(member: Member, role: Role) {
  await orgApi.setRole(member.user_id, role)
  await reload()
  ElMessage.success(`${member.username} 现在是${ROLE_META[role].label}`)
}

async function removeMember(member: Member) {
  await ElMessageBox.confirm(
    `把 ${member.username} 移出组织？他上传过的文档会留下，署名也保留。`,
    '移除成员',
    { type: 'warning', confirmButtonText: '移除', cancelButtonText: '取消' },
  )
  await orgApi.removeMember(member.user_id)
  await reload()
  ElMessage.success('已移除')
}
</script>

<template>
  <section class="members">
    <header>
      <div>
        <h2>{{ org?.name ?? '组织' }}</h2>
        <p class="hint">
          一次部署 = 一份语料。组织内成员共享全部文档，角色控制的是能做什么，
          不是能看见什么。
        </p>
      </div>
      <el-tag v-if="auth.role" type="info">你是{{ ROLE_META[auth.role].label }}</el-tag>
    </header>

    <el-alert
      v-if="!auth.canManageOrg"
      type="info"
      :closable="false"
      title="只有管理员能改成员与角色"
      description="你可以看到成员列表，但下面的操作对你是只读的。"
    />

    <div v-if="auth.canManageOrg" class="invite">
      <el-input v-model="newUsername" placeholder="已有账号的用户名" class="name"
                @keyup.enter="addMember" />
      <el-select v-model="newRole" class="role">
        <el-option v-for="option in roleOptions" :key="option.value"
                   :value="option.value" :label="option.label" />
      </el-select>
      <el-button type="primary" :loading="inviting" @click="addMember">加入组织</el-button>
    </div>

    <el-table :data="members" v-loading="loading" class="table">
      <el-table-column prop="username" label="成员" min-width="160" />
      <el-table-column prop="email" label="邮箱" min-width="180">
        <template #default="{ row }">{{ row.email || '—' }}</template>
      </el-table-column>
      <el-table-column label="角色" width="180">
        <template #default="{ row }">
          <el-select
            v-if="auth.canManageOrg"
            :model-value="row.role"
            :disabled="isLastAdmin(row)"
            @update:model-value="(value: Role) => changeRole(row, value)"
          >
            <el-option v-for="option in roleOptions" :key="option.value"
                       :value="option.value" :label="option.label" />
          </el-select>
          <span v-else>{{ ROLE_META[row.role as Role].label }}</span>
        </template>
      </el-table-column>
      <el-table-column label="加入时间" width="180">
        <template #default="{ row }">{{ new Date(row.joined_at).toLocaleDateString() }}</template>
      </el-table-column>
      <el-table-column label="操作" width="120" v-if="auth.canManageOrg">
        <template #default="{ row }">
          <el-tooltip v-if="isLastAdmin(row)"
                      content="组织必须至少有一个管理员 —— 降级或移除最后一个会让它永久失去管理能力">
            <span><el-button link disabled>移除</el-button></span>
          </el-tooltip>
          <el-button v-else link type="danger" @click="removeMember(row)">移除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>

<style scoped>
.members {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

h2 {
  margin: 0 0 4px;
}

.hint {
  margin: 0;
  color: var(--ddp-text-muted, #888);
  font-size: 13px;
  max-width: 60ch;
}

.invite {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.invite .name {
  flex: 1 1 240px;
}

.invite .role {
  width: 160px;
}

.table {
  width: 100%;
}
</style>
