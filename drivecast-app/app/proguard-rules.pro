# kotlinx.serialization
-keepattributes *Annotation*, InnerClasses
-dontnote kotlinx.serialization.**
-keepclassmembers class **$$serializer { *; }
-keepclasseswithmembers class com.drivecast.tv.api.** {
    kotlinx.serialization.KSerializer serializer(...);
}
-keep,includedescriptorclasses class com.drivecast.tv.api.**$$serializer { *; }
-keepclassmembers @kotlinx.serialization.Serializable class com.drivecast.tv.** { *; }

# kotlinx.serialization README recipe: keep Companion + serializer() lookups for any
# @Serializable class app-wide (belt-and-braces alongside the api.** rules above).
-if @kotlinx.serialization.Serializable class com.drivecast.tv.**
-keepclassmembers class <1> {
    static <1>$Companion Companion;
}
-if @kotlinx.serialization.Serializable class com.drivecast.tv.** {
    static **$* *;
}
-keepclassmembers class <2>$* {
    kotlinx.serialization.KSerializer serializer(...);
}

# Retrofit 2 (standard rules: https://github.com/square/retrofit/blob/master/retrofit/src/main/resources/META-INF/proguard/retrofit2.pro)
-keepattributes Signature, Exceptions
-keepattributes RuntimeVisibleAnnotations, RuntimeVisibleParameterAnnotations, AnnotationDefault
-keepclasseswithmembers,allowobfuscation interface * {
    @retrofit2.http.* <methods>;
}
-dontwarn okhttp3.**
-dontwarn okio.**
-dontwarn retrofit2.**
-dontwarn javax.annotation.**
-dontwarn org.codehaus.mojo.animal_sniffer.*
