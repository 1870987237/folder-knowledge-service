import type { ThemeConfig } from 'antd';
export const adminTheme: ThemeConfig = {
  token: {
    colorPrimary: '#167367', colorPrimaryBg: '#eaf4f1', colorInfo: '#167367', colorSuccess: '#2f7a53',
    colorWarning: '#ad7624', colorError: '#b33f42', colorText: '#243438',
    // Muted brand colors need explicit light surfaces; generated palettes are too dark.
    colorSuccessBg: '#f0f9f2', colorSuccessBgHover: '#e2f2e5',
    colorSuccessBorder: '#c6e4ce', colorSuccessBorderHover: '#a7d2b4', colorSuccessText: '#2f7a53',
    colorWarningBg: '#fff8e8', colorWarningBgHover: '#fff0c9',
    colorWarningBorder: '#efd69e', colorWarningBorderHover: '#dfc27d', colorWarningText: '#ad7624',
    colorErrorBg: '#fff2f0', colorErrorBgHover: '#ffe7e4',
    colorErrorBorder: '#f3cbc9', colorErrorBorderHover: '#e7adab', colorErrorText: '#b33f42',
    colorInfoBg: '#eaf4f1', colorInfoBgHover: '#dbece6',
    colorInfoBorder: '#c3ded6', colorInfoBorderHover: '#a3cabf', colorInfoText: '#167367',
    colorTextSecondary: '#68777d', colorBgLayout: '#f5f7f8', colorBorderSecondary: '#e7edef',
    borderRadius: 8, fontSize: 14, controlHeight: 36,
    fontFamily: '"Segoe UI", "Microsoft YaHei", sans-serif',
  },
  components: {
    Layout: { siderBg: '#ffffff', bodyBg: '#f5f7f8' },
    Menu: { itemHeight: 42, itemSelectedBg: '#eaf4f1', itemSelectedColor: '#14695e' },
    Table: { headerBg: '#fafbfc', cellPaddingBlock: 16 },
    Typography: { titleMarginTop: 0, titleMarginBottom: 0 },
  },
};
