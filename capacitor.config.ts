import { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.blackchat.app',
  appName: 'Black Chat',
  webDir: 'dist',
  server: {
    androidScheme: 'https',
    cleartext: true,
  },
  ios: {
    preferredContentMode: 'mobile',
  },
  android: {
    allowMixedContent: true,
    backgroundColor: '#1a1a1a',
    buildOptions: {
      releaseType: 'APK',
    },
  },
  plugins: {
    SplashScreen: {
      launchAutoHide: true,
      backgroundColor: '#1a1a1a',
      androidScaleType: 'CENTER_CROP',
      androidSpinnerBarColor: '#ffffff',
      showSpinner: true,
      spinnerStyle: 'large',
      splashFullScreen: true,
      splashImmersiveMode: false,
    },
    StatusBar: {
      style: 'DARK',
      backgroundColor: '#1a1a1a',
    },
  },
};

export default config;
