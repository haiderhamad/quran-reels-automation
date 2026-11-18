import { SettingsForm } from '@/components/SettingsForm';
import { StoredConfigSchema } from '@/lib/schema';
import type { SystemConnectionStatus } from '@/lib/system';
export default function Page() {
 const status: SystemConnectionStatus = { b2:false,github:false,quranFoundation:false,pexels:false,metaFacebook:false,metaInstagram:false,publisherBackend:'disabled',bufferFacebook:false,bufferInstagram:false,bufferPublishingEnabled:false,reciterReuseRuntimeGate:false,englishTranslation:true,germanTranslation:true,audioYasserTiming:true,scheduler:'not_activated',schedulerHealth:{status:'PAUSED',automation:'OFF',trigger:'Offline QA',lastHeartbeat:null,lastHeartbeatSource:null,lastClaim:null,lastScheduledRun:null},previewCatalog:'ready'};
 return <main style={{padding:24,maxWidth:1600,margin:'auto'}}><SettingsForm initialConfig={StoredConfigSchema.parse({})} systemStatus={status} configWritable={false}/></main>;
}
