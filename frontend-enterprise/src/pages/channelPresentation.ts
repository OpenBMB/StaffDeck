export type ChannelPresentation = {
  name: string;
  identifierLabel: string;
  userLabel: string;
  blurb: string;
  disconnectDescription: string;
};

const BUILT_IN_CHANNELS: Record<string, ChannelPresentation> = {
  wechat: {
    name: '微信',
    identifierLabel: '机器人 ID',
    userLabel: '微信用户',
    blurb: '扫码接入，微信用户直接与数字员工对话。',
    disconnectDescription: '断开后微信接入将离线，需要重新扫码才能恢复；对话记录保留。确定断开接入吗？',
  },
  wecom: {
    name: '企业微信',
    identifierLabel: '机器人 ID',
    userLabel: '企业微信用户',
    blurb: '填入企业微信智能机器人的凭证完成接入。',
    disconnectDescription: '断开后企业微信接入将停止服务，需要重新配置凭证才能恢复；对话记录保留。确定断开接入吗？',
  },
  feishu: {
    name: '飞书',
    identifierLabel: 'App ID',
    userLabel: '飞书用户',
    blurb: '填入飞书应用凭证，通过长连接接入数字员工。',
    disconnectDescription: '断开后飞书接入将停止服务，需要重新配置应用凭证才能恢复；对话记录保留。确定断开接入吗？',
  },
  dingtalk: {
    name: '钉钉',
    identifierLabel: 'Client ID',
    userLabel: '钉钉用户',
    blurb: '填入钉钉 Stream 应用凭证，通过长连接接入数字员工。',
    disconnectDescription: '断开后钉钉接入将停止服务，需要重新配置应用凭证才能恢复；对话记录保留。确定断开接入吗？',
  },
};

export function getChannelPresentation(channel: string, configuredName?: string): ChannelPresentation {
  const key = channel.trim().toLowerCase();
  const preset = BUILT_IN_CHANNELS[key];
  const name = configuredName?.trim() || preset?.name || (key || '渠道');
  return {
    name,
    identifierLabel: preset?.identifierLabel || '渠道标识',
    userLabel: preset?.userLabel || `${name}用户`,
    blurb: preset?.blurb || `通过${name}与数字员工对话。`,
    disconnectDescription:
      preset?.disconnectDescription ||
      `断开后${name}接入将停止服务，需要重新配置该渠道才能恢复；对话记录保留。确定断开接入吗？`,
  };
}
